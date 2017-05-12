# -*- coding: utf-8 -*-
"""
Data extraction and transfer from Miniseed files
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
from future.builtins import *  # NOQA
from collections import namedtuple
from obspy.core import UTCDateTime
import bisect
from portable_fdsnws_dataselect.msriterator import MSR_iterator
from logging import getLogger
from obspy import read as mseed_read
from _io import BytesIO
import ctypes
from obspy.core.stream import Stream
import re
import os

logger = getLogger(__name__)


class NoDataError(Exception):
    """
    Error raised when no data is found
    """
    pass


class RequestLimitExceededError(Exception):
    """
    Error raised when the amount of data exceeds the configured limit
    """
    pass


class ExtractedDataSegment(object):
    """
    There are a few different forms that a chunk of extracted data can take, so we return
    a wrapped object that exposes a simple, consistent API for the handler to use.
    """
    def write(self, wfile):
        """
        Write the data to the given file-like object
        """
        raise NotImplementedError()

    def get_num_bytes(self):
        """
        Return the number of bytes in the segment
        """
        raise NotImplementedError()

    def get_src_name(self):
        """
        Return the name of the data source
        """
        raise NotImplementedError()


class MSRIDataSegment(ExtractedDataSegment):
    """
    Segment of data from a MSR_iterator
    """
    def __init__(self, msri, sample_rate, start_time, end_time, src_name):
        """
        :param msri: A `MSR_iterator`
        :param sample_rate: Sample rate of the data
        :param start_time: A `UTCDateTime` giving the start of the requested data
        :param end_time: A `UTCDateTime` giving the end of the requested data
        :param src_name: Name of the data source for logging
        """
        self.msri = msri
        self.sample_rate = sample_rate
        self.start_time = start_time
        self.end_time = end_time
        self.src_name = src_name

    def write(self, wfile):
        msrstart = self.msri.get_startepoch()
        msrend = self.msri.get_endepoch()
        reclen = self.msri.msr.contents.reclen

        sepoch = self.start_time.timestamp
        eepoch = self.end_time.timestamp

        # Process records that intersect with request time window
        if msrstart < eepoch and msrend > sepoch:

            # Trim record if coverage and partial overlap with request
            if self.sample_rate > 0 and (msrstart < self.start_time or msrend > self.end_time):
                logger.debug("Trimming record %s @ %s" % (self.src_name, self.msri.get_starttime()))
                tr = mseed_read(BytesIO(ctypes.string_at(self.msri.msr.contents.record, reclen)), format="MSEED")[0]
                tr.trim(self.start_time, self.end_time)
                st = Stream(traces=[tr])

                st.write(wfile, format="MSEED")

            # Otherwise, write un-trimmed record
            else:
                # Construct to avoid copying the data, supposedly
                wfile.write((ctypes.c_char * reclen).
                                 from_address(ctypes.addressof(self.msri.msr.contents.record.contents)))

    def get_num_bytes(self):
        return self.msri.msr.contents.reclen

    def get_src_name(self):
        return self.src_name


class FileDataSegment(ExtractedDataSegment):
    """
    Segment of data that comes directly from a data file
    """
    def __init__(self, filename, start_byte, num_bytes, src_name):
        """
        :param filename: Name of data file
        :param start_byte: Return data starting from this offset
        :param num_bytes: Length of data to return
        :param src_name: Name of the data source for logging
        """
        self.filename = filename
        self.start_byte = start_byte
        self.num_bytes = num_bytes
        self.src_name = src_name

    def write(self, wfile):
        with open(self.filename, "rb") as f:
            f.seek(self.start_byte)
            raw_data = f.read(self.num_bytes)
            wfile.write(raw_data)

    def get_num_bytes(self):
        return self.num_bytes

    def get_src_name(self):
        return self.src_name


class MiniseedDataExtractor(object):
    """
    Component for extracting, trimming, and validating data.
    """
    def __init__(self, dp_replace=None, request_limit=0):
        """
        :param dp_replace: optional tuple of (regex, replacement) indicating the location of data files
        :param request_limit: optional limit (in bytes) on how much data can be extracted at once
        """
        if dp_replace:
            self.dp_replace_re = re.compile(dp_replace[0])
            self.dp_replace_sub = dp_replace[1]
        else:
            self.dp_replace_re = None
            self.dp_replace_sub = None
        self.request_limit = request_limit

    def handle_trimming(self, stime, etime, row):
        """
        Get the time & byte-offsets for the data in time range (stime, etime).

        This is done by finding the smallest section of the data in row that falls within the desired time range
        and is identified by the timeindex field of row

        :returns: [(start time, start offset), (end time, end offset)]
        """
        etime = UTCDateTime(row[20])
        row_stime = UTCDateTime(row[5])
        row_etime = UTCDateTime(row[6])

        # If we need a subset of the this block, trim it accordingly
        block_start = int(row[9])
        block_end = block_start + int(row[10])
        if stime > row_stime or etime < row_etime:
            tix = [x.split("=>") for x in row[12].split(",")]
            if tix[-1][0] == 'latest':
                tix[-1] = [str(row_etime.timestamp), block_end]
            to_x = [float(x[0]) for x in tix]
            s_index = bisect.bisect_right(to_x, stime.timestamp) - 1
            if s_index < 0:
                s_index = 0
            e_index = bisect.bisect_right(to_x, etime.timestamp)
            off_start = int(tix[s_index][1])
            if e_index >= len(tix):
                e_index = -1
            off_end = int(tix[e_index][1])
            return ([to_x[s_index], off_start, stime > row_stime], [to_x[e_index], off_end, etime < row_etime],)
        else:
            return ([row_stime.timestamp, block_start, False], [row_etime.timestamp, block_end, False])

    def extract_data(self, index_rows):
        """
        Perform the data extraction.

        :param index_rows: requested data, as produced by `HTTPServer_RequestHandler.fetch_index_rows`
        :yields: sequence of `ExtractedDataSegment`s
        """

        # Pre-scan the index rows:
        # 1) Build processed list for extraction
        # 2) Check if the request is small enough to satisfy
        # Note: accumulated estimate of output bytes will be equal to or higher than actual output
        total_bytes = 0
        request_rows = []
        Request = namedtuple('Request',['src_name','file_name','start_time','end_time',
                                        'trim_info','section_bytes','sample_rate'])
        if self.request_limit > 0:
            try:
                for row in index_rows:
                    file_name = row[8]
                    start_time = UTCDateTime(row[19])
                    end_time = UTCDateTime(row[20])
                    trim_info = self.handle_trimming(start_time, end_time, row)
                    total_bytes += trim_info[1][1] - trim_info[0][1]
                    if total_bytes > self.request_limit:
                        raise RequestLimitExceededError("Result exceeds limit of %d bytes" % self.request_limit)
                    if self.dp_replace_re:
                        file_name = self.dp_replace_re.sub(self.dp_replace_sub, file_name)
                    if not os.path.exists(file_name):
                        raise Exception("Data file does not exist: %s" % file_name)
                    request_rows.append(Request(src_name="_".join(row[:4]),
                                                file_name = file_name,
                                                start_time = start_time,
                                                end_time = end_time,
                                                trim_info = trim_info,
                                                section_bytes = row[10],
                                                sample_rate = row[7]))
            except Exception as err:
                import traceback
                traceback.print_exc()
                raise Exception("Error accessing data index: %s" % str(err))

        # Error if request matches no data
        if total_bytes == 0:
            raise NoDataError()

        # Get & return the actual data
        for row in request_rows:
            logger.debug ("Extracting %s (%s - %s) from %s" % (row.src_name, row.start_time, row.end_time, row.file_name))

            # Iterate through records in section if only part of the section is needed
            if row.trim_info[0][2] or row.trim_info[1][2]:

                for msri in MSR_iterator(filename=row.file_name, startoffset=row.trim_info[0][1], dataflag=False):
                    offset = msri.get_offset()

                    # Done if we are beyond end offset
                    if offset >= row.trim_info[1][1]:
                        break

                    yield MSRIDataSegment(msri, row.sample_rate, row.start_time, row.end_time, row.src_name)

                    # Check for passing end offset
                    if (offset + msri.msr.contents.reclen) >= row.trim_info[1][1]:
                        break

            # Otherwise, return the entire section
            else:
                yield FileDataSegment(row.file_name, row.trim_info[0][1], row.section_bytes, row.src_name)