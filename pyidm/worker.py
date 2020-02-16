"""
    pyIDM

    multi-connections internet download manager, based on "pyCuRL/curl", "youtube_dl", and "PySimpleGUI"

    :copyright: (c) 2019-2020 by Mahmoud Elshahat.
    :license: GNU LGPLv3, see LICENSE for more details.
"""

# worker class
import os
import time

import certifi
import pycurl

from .config import Status, APP_NAME, proxy
from .utils import get_seg_size


class Worker:
    def __init__(self, tag=0, d=None):
        self.tag = tag
        self.d = d
        self.q = d.q
        self.seg = None
        self.resume_range = None

        # writing data parameters
        self.file = None
        self.mode = 'wb'  # file opening mode default to new write binary

        self.downloaded = 0
        self.start_size = 0  # initial file size before start resuming

        # connection parameters
        self.c = pycurl.Curl()
        self.speed_limit = 0
        self.headers = {}

    @property
    def current_filesize(self):
        return self.start_size + self.downloaded

    def reuse(self, seg=None, speed_limit=0):
        """Recycle same object again, better for performance as recommended by curl docs"""
        self.reset()

        self.seg = seg
        self.speed_limit = speed_limit

        self.q.log('start worker', self.tag, 'seg', self.seg.num, 'range:', self.seg.range, 'SL=', self.speed_limit)

        # get start file size if this segment file partially downloaded before
        if os.path.exists(self.seg.name) and self.seg.size and self.seg.range:
            with open(self.seg.name, 'rb') as f:
                self.start_size = len(f.read())

            # try to get missing segment size, if this is a fresh file, segment size will be updated from
            # self.header_callback() method
            if not self.seg.size:
                self.seg.get_size()

            self.check_previous_download()

        # print(self.seg.url)

    def reset(self):
        # reset curl
        self.c.reset()

        # reset variables
        self.start_size = 0
        self.file = None
        self.mode = 'wb'  # file opening mode default to new write binary
        self.downloaded = 0
        self.resume_range = None

    def check_previous_download(self):

        if self.current_filesize == self.seg.size:  # segment is completed before
            # self.report_completed()
            self.q.log('worker', self.tag, ': File', self.seg.num, 'already completed before')
            self.seg.downloaded = True

        # in case the server sent extra bytes from last session by mistake, truncate file
        elif self.current_filesize > self.seg.size:
            self.q.log(f"found seg {self.seg.num} over-sized {self.current_filesize}")

            # truncate file
            with open(self.seg.name, 'rb+') as f:
                f.truncate(self.seg.size)
            # self.report_completed()
            self.seg.downloaded = True

        else:  # should resume
            # set new range and file open mode
            a, b = [int(x) for x in self.seg.range.split('-')]
            self.resume_range = f'{a + self.current_filesize}-{b}'
            self.mode = 'ab'  # open file for append

            # report
            self.q.log('Thread', self.tag, ': File', self.seg.num, 'resuming, new range:', self.resume_range,
                       'current file size:', self.current_filesize)

    def verify(self):
        """check if segment completed"""
        # print('self.current_filesize =', self.current_filesize,  "self.seg.size", self.seg.size)
        if self.current_filesize == self.seg.size or self.seg.size == 0:
            return True
        else:
            return False

    def report_not_completed(self):
        self.q.log('worker', self.tag, 'did not complete', self.seg.name, 'downloaded',
                   self.current_filesize, 'target size:', self.seg.size, 'remaining:',
                   self.seg.size - self.current_filesize)

        # put back to jobs queue to try again
        self.q.jobs.put(self.seg)

    def report_completed(self):
        self.q.log('worker', self.tag, 'completed', self.seg.name)
        self.seg.downloaded = True

        self.q.log('downloaded: ', self.seg.name)
        # print(self.headers)

    def set_options(self):
        # todo: change agent name
        agent = f"{APP_NAME} Download Manager"
        self.c.setopt(pycurl.USERAGENT, agent)

        self.c.setopt(pycurl.URL, self.seg.url)

        range_ = self.resume_range or self.seg.range
        if range_:
            self.c.setopt(pycurl.RANGE, range_)  # download segment only not the whole file

        # set proxy, must be string empty '' means no proxy
        self.c.setopt(pycurl.PROXY, proxy)

        # re-directions
        self.c.setopt(pycurl.FOLLOWLOCATION, 1)
        self.c.setopt(pycurl.MAXREDIRS, 10)

        self.c.setopt(pycurl.NOSIGNAL, 1)  # option required for multithreading safety
        self.c.setopt(pycurl.NOPROGRESS, 0)  # will use a progress function
        self.c.setopt(pycurl.CAINFO, certifi.where())  # for https sites and ssl cert handling

        # set speed limit selected by user
        self.c.setopt(pycurl.MAX_RECV_SPEED_LARGE, self.speed_limit)  # cap download speed to n bytes/sec, 0=disabled

        # time out
        self.c.setopt(pycurl.CONNECTTIMEOUT, 30)  # limits the connection phase, it has no impact once it has connected.
        # self.c.setopt(pycurl.TIMEOUT, 300)  # limits the whole operation time

        # abort if download speed slower than 1 byte/sec during 60 seconds
        self.c.setopt(pycurl.LOW_SPEED_LIMIT, 1)
        self.c.setopt(pycurl.LOW_SPEED_TIME, 60)

        # verbose
        self.c.setopt(pycurl.VERBOSE, 0)

        # it tells curl not to include headers with the body
        self.c.setopt(pycurl.HEADEROPT, 0)

        # call back functions
        self.c.setopt(pycurl.HEADERFUNCTION, self.header_callback)
        self.c.setopt(pycurl.WRITEFUNCTION, self.write)
        self.c.setopt(pycurl.XFERINFOFUNCTION, self.progress)

    def header_callback(self, header_line):
        header_line = header_line.decode('iso-8859-1')
        header_line = header_line.lower()

        if ':' not in header_line:
            return

        name, value = header_line.split(':', 1)
        name = name.strip()
        value = value.strip()
        self.headers[name] = value

        # update segment size if not available
        if not self.seg.size and name == 'content-length':
            try:
                self.seg.size = int(self.headers.get('content-length', 0))
                # print('self.seg.size = ', self.seg.size)
            except:
                pass

    def progress(self, *args):
        """it receives progress from curl and can be used as a kill switch
        Returning a non-zero value from this callback will cause curl to abort the transfer
        """

        # check termination by user
        if self.d.status != Status.downloading:
            return -1  # abort

    def run(self):
        # check if file completed before and exit
        if self.seg.downloaded:
            return

        self.set_options()

        # make sure target directory exist
        target_directory = os.path.dirname(self.seg.name)
        if not os.path.isdir(target_directory):
            os.makedirs(target_directory)  # it will also create any intermediate folders in the given path

        try:
            with open(self.seg.name, self.mode) as self.file:
                self.c.perform()

            # print('worker', self.tag, 'curl done')

            completed = self.verify()
            if completed:
                self.report_completed()
                # print('worker', self.tag, 'completed')
            else:
                self.report_not_completed()

            response_code = self.c.getinfo(pycurl.RESPONSE_CODE)
            if response_code in range(400, 512):
                self.q.log('server refuse connection', response_code, 'cancel download and try to refresh link')
                self.q.brain.put(('server', ['error', response_code]))

        except Exception as e:
            if any(statement in repr(e) for statement in ('Failed writing body', 'Callback aborted')):
                error = f'worker {self.tag} terminated, {e}'
            else:
                error = repr(e)

            self.q.log('worker', self.tag, ': quitting ...', error)
            self.report_not_completed()

    def write(self, data):
        """write to file"""
        self.file.write(data)
        self.downloaded += len(data)

        self.d.downloaded += len(data)

        # check if we getting over sized
        if self.current_filesize > self.seg.size > 0:
            return -1  # abort



