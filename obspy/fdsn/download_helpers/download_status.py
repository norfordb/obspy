#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Class representing the current status of a download in progress.

Intended to simplify and stabilize the logic of the download helpers and make
it understandable in the first place.

:copyright:
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2014
:license:
    GNU Lesser General Public License, Version 3
    (http://www.gnu.org/copyleft/lesser.html)
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
from future.builtins import *  # NOQA
from future import standard_library
with standard_library.hooks():
    import itertools


import collections
import copy
from multiprocessing.pool import ThreadPool
import os
import time
import timeit

import obspy
from obspy.core.util import Enum

from . import utils

# Some application.wadls are wrong...
OVERWRITE_CAPABILITIES = {
    "resif": None
}


# The current status of an entity.
STATUS = Enum(["none", "needs_downloading", "downloaded", "ignore", "exists",
               "download_failed", "download_rejected"])


class Station(object):
    """
    Object representing a seismic station within the download helper classes.

    It knows the coordinates of the station to perform the filtering,
    its channels and the filename and status of the StationXML files.

    :param network: The network code.
    :type network: str
    :param station: The station code.
    :type station: str
    :param latitude: The latitude of the station.
    :type latitude: float
    :param longitude: The longitude of the station.
    :type longitude: float
    :param channels: The channels of the station.
    :type channels: list of :class:`~.Channel` objects
    :param stationxml_filename: The filename of the StationXML file.
    :type stationxml_filename: str
    """
    __slots__ = ["network", "station", "latitude", "longitude", "channels",
                 "_stationxml_filename", "want_station_information",
                 "miss_station_information", "have_station_information"]

    def __init__(self, network, station, latitude, longitude, channels,
                 stationxml_filename=None):
        # Station attributes.
        self.network = network
        self.station = station
        self.latitude = latitude
        self.longitude = longitude
        self.channels = channels
        # Station information settings.
        self.stationxml_filename = stationxml_filename

        # Internally keep track of which channels and time interval want
        # station information, which miss station information and which
        # already have some. want_station_information should always be the
        # union of miss and have.
        self.want_station_information = {}
        self.miss_station_information = {}
        self.have_station_information = {}

    @property
    def stationxml_filename(self):
        return self._stationxml_filename

    @stationxml_filename.setter
    def stationxml_filename(self, value):
        self._stationxml_filename = value
        if not value:
            return
        dirname = os.path.dirname(value)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

    @property
    def temporal_bounds(self):
        """
        Return the temporal bounds for the station.
        """
        starttimes = []
        endtimes = []
        for channel in self.channels:
            s, e = channel.temporal_bounds
            starttimes.append(s)
            endtimes.append(e)
        return min(starttimes), max(endtimes)

    def __str__(self):
        channels = "\n".join(str(i) for i in self.channels)
        channels = "\n\t".join(channels.splitlines())
        return (
            "Station '{network}.{station}' [Lat: {lat:.2f}, Lng: {lng:.2f}]\n"
            "\t-> Filename: {filename} ({status})\n"
            "\t-> Wants station information for channels:  {want}\n"
            "\t-> Has station information for channels:    {has}\n"
            "\t-> Misses station information for channels: {miss}\n"
            "\t{channels}"
        ).format(
            network=self.network,
            station=self.station,
            lat=self.latitude,
            lng=self.longitude,
            filename=self.stationxml_filename,
            status="exist" if (self.stationxml_filename and os.path.exists(
                self.stationxml_filename)) else "does not yet exist",
            want=", ".join(["%s.%s" % (_i[0], _i[1]) for _i in
                            self.want_station_information.keys()]),
            has=", ".join(["%s.%s" % (_i[0], _i[1]) for _i in
                          self.have_station_information.keys()]),
            miss=", ".join(["%s.%s" % (_i[0], _i[1]) for _i in
                           self.miss_station_information.keys()]),
            channels=channels)

    def prepare_stationxml_download(self, stationxml_storage):
        """
        Figure out what to download.

        :param stationxml_storage:
        """
        # Determine what channels actually want to have station information.
        # This will be a tuple of location code, channel code, starttime,
        # and endtime.
        self.want_station_information = {}
        for channel in self.channels:
            if channel.wants_station_information is False:
                continue
            self.want_station_information[
                (channel.location, channel.channel)] = channel.temporal_bounds

        # Parse user passed information.
        s, e = self.temporal_bounds
        storage = utils.get_stationxml_filename(
            stationxml_storage, self.network, self.station,
            [(_i.location, _i.channel) for _i in self.channels],
            starttime=s, endtime=e)

        # The simplest case. The function returns a string. Now two things
        # can happen.
        if isinstance(storage, (str, bytes)):
            filename = storage
            self.stationxml_filename = filename
            # 1. The file does not yet exist. Thus all channels must be
            # downloaded new.
            if not os.path.exists(filename):
                self.miss_station_information = \
                    copy.deepcopy(self.want_station_information)
                self.have_station_information = {}
            # 2. The file does exist. It will be parsed. If it contains ALL
            # necessary information, nothing will happen. Otherwise it will
            # be overwritten.
            else:
                info = utils.get_stationxml_contents(filename)
                for c_id, times in self.want_station_information.items():
                    # Get the temporal range of information in the file.
                    c_info = [_i for _i in info if
                              _i.network == self.network and
                              _i.station == self.station and
                              _i.location == c_id[0] and
                              _i.channel == c_id[1]]
                    if not c_info:
                        break
                    starttime = min([_i.starttime for _i in c_info])
                    endtime = max([_i.endtime for _i in c_info])
                    if starttime > times[0] or endtime < times[1]:
                        break
                # All good if no break is called.
                else:
                    self.have_station_information = \
                        copy.deepcopy(self.want_station_information)
                    self.miss_station_information = {}
                    return
                # Otherwise everything will be downloaded.
                self.miss_station_information = \
                    copy.deepcopy(self.want_station_information)
                self.have_station_information = {}
        # The other possibility is that a dictionary is returned.
        else:
            raise NotImplementedError

    def prepare_mseed_download(self, mseed_storage):
        """
        Loop through all channels of the station and distribute filenames
        and the current status of the channel.

        A MiniSEED interval will be ignored, if the `mseed_storage` function
        returns `True`.
        Possible statuses after this the method execution are IGNORE, EXISTS,
        and NEEDS_DOWNLOADING.

        :param mseed_storage:
        """
        for channel in self.channels:
            for interval in channel.intervals:
                interval.filename = utils.get_mseed_filename(
                    mseed_storage, self.network, self.station,
                    channel.location, channel.channel, interval.start,
                    interval.end)
                if interval.filename is True:
                    interval.status = STATUS.IGNORE
                elif os.path.exists(interval.filename):
                    interval.status = STATUS.EXISTS
                else:
                    if not os.path.exists(os.path.dirname(interval.filename)):
                        os.makedirs(os.path.dirname(interval.filename))
                    interval.status = STATUS.NEEDS_DOWNLOADING

    def sanitize_downloads(self, logger):
        """
        Should be run after the MiniSEED and StationXML downloads finished.
        It will make sure that every MiniSEED file also has a corresponding
        StationXML file.
        """
        # All or nothing for each channel.
        for id in self.miss_station_information:
            logger.warnings("No station information could be downloaded for "
                            "%s.%s.%s.%s. All downloaded MiniSEED files "
                            "will be deleted!" % (
                                self.network, self.station, id[0], id[1]))
            channel = self.channels[id]
            for time_interval in channel.intervals:
                # Only delete downloaded things!
                if time_interval.status == STATUS.DOWNLOADED:
                    utils.safe_delete(time_interval.filename)
                    time_interval.status = STATUS.DOWNLOAD_REJECTED


class Channel(object):
    """
    Object representing a Channel. Each time interval should end up in one
    MiniSEED file.
    """
    __slots__ = ["location", "channel", "intervals"]

    def __init__(self, location, channel, intervals):
        self.location = location
        self.channel = channel
        self.intervals = intervals

    @property
    def wants_station_information(self):
        """
        Determine if the channel requires any station information.

        As soon as at the status of at least one interval is either
        ``DOWNLOADED`` or ``EXISTS`` the whole channel will be thought of as
        requiring station information. This does not yet mean that station
        information will be downloaded. That is decided at a later stage.
        """
        status = set([_i.status for _i in self.intervals])
        if STATUS.DOWNLOADED in status or STATUS.EXISTS in status:
            return True
        return False

    @property
    def temporal_bounds(self):
        """
        Returns a tuple of the minimum start time and the maximum end time.
        """
        return (min([_i.start for _i in self.intervals]),
                max([_i.end for _i in self.intervals]))

    @property
    def intervals_need_station_file(self):
        """
        Returns True if at least one interval has either status DOWNLOADED
        or EXISTS.
        """
        valid_status = [STATUS.DOWNLOADED, STATUS.EXISTS]
        for interval in self.intervals:
            if interval.status in valid_status:
                return True
        return False

    def __str__(self):
        return "Channel '{location}.{channel}:'\n\t{intervals}".format(
            location=self.location, channel=self.channel,
            intervals="\n\t".join([str(i) for i in self.intervals]))


class TimeInterval(object):
    """
    Simple object representing a time inverval of a channel.

    It knows the temporal bounds of the interval, the (desired) filename,
    and the current status of the interval.

    :param start: The start of the interval.
    :type start: :class:`~obspy.core.utcdatetime.UTCDateTime`
    :param end: The end of the interval.
    :type end: :class:`~obspy.core.utcdatetime.UTCDateTime`
    :param filename: The filename of the interval.
    :type filename: str
    :param status: The status of the time interval.
    :param status: :class:`~.STATUS`
    """
    __slots__ = ["start", "end", "filename", "status"]

    def __init__(self, start, end, filename=None, status=None):
        self.start = start
        self.end = end
        self.filename = filename
        self.status = status if status is not None else STATUS.NONE

    def __repr__(self):
        return "TimeInterval(start={start}, end={end}, filename={filename}, " \
               "status={status})".format(
                   start=repr(self.start),
                   end=repr(self.end),
                   filename=repr(self.filename),
                   status=repr(self.status))


class ClientDownloadHelper(object):
    """
    :type client: :class:`obspy.fdsn.client.Client`
    :param client: An initialized FDSN client.
    :type client_name: str
    :param client_name: The name of the client. Only used for logging.
    :type restrictions: :class:`obspy.fdsn.download_helpers.Restrictions`
    :param restrictions: The non-domain related restrictions for the query.
    :type domain: :class:`obspy.fdsn.download_helpers.Domain` subclass
    :param domain: The domain definition.
    :rtype: dict
    """
    def __init__(self, client, client_name, restrictions, domain,
                 mseed_storage, stationxml_storage, logger):
        self.client = client
        self.client_name = client_name
        self.restrictions = restrictions
        self.domain = domain
        self.mseed_storage = mseed_storage
        self.stationxml_storage = stationxml_storage
        self.logger = logger
        self.stations = {}
        self.is_availability_reliable = None

    def __bool__(self):
        return bool(len(self))

    def __str__(self):
        if self.is_availability_reliable is None:
            reliability = "Unknown reliability of availability information"
        elif self.is_availability_reliable is True:
            reliability = "Reliable availability information"
        elif self.is_availability_reliable is False:
            reliability = "Non-reliable availability information"
        else:
            raise NotImplementedError
        return (
            "ClientDownloadHelper object for client '{client}' ({url})\n"
            "-> {reliability}\n"
            "-> Manages {station_count} stations.\n{stations}").format(
                client=self.client_name,
                url=self.client.base_url,
                reliability=reliability,
                station_count=len(self),
                stations="\n".join([str(_i) for _i in self.stations.values()]))

    def __len__(self):
        return len(self.stations)

    def prepare_mseed_download(self):
        """
        Prepare each Station for the MiniSEED downloading stage.

        This will distribute filenames and identify files that require
        downloading.
        """
        for station in self.stations.values():
            station.prepare_mseed_download(mseed_storage=self.mseed_storage)

    def prepare_stationxml_download(self):
        """
        Prepare each Station for the StationXML downloading stage.

        This will distribute filenames and identify files that require
        downloading.
        """
        for station in self.stations.values():
            station.prepare_stationxml_download(
                stationxml_storage=self.stationxml_storage)

    def download_stationxml(self, threads=10):
        """
        Actually download the StationXML files.

        :param threads: Limits the maximum number of threads for the client.
        """

        def star_download_station(args):
            """
            Maps arguments to the utils.download_stationxml() function.

            :param args: The to-be mapped arguments.
            """
            try:
                ret_val = utils.download_stationxml(*args, logger=self.logger)
            except utils.ERRORS as e:
                self.logger.error(str(e))
                return None
            return ret_val

        arguments = []
        for station in self.stations.values():
            if not station.miss_station_information:
                continue
            s, e = station.temporal_bounds
            bulk = [(station.network, station.station, channel.location,
                     channel.channel, s, e) for channel in station.channels]
            arguments.append((self.client, self.client_name, bulk,
                              station.stationxml_filename))

        if not arguments:
            self.logger.info("Client '%s' - No station information to "
                             "download." % self.client_name)
            return

        s_time = timeit.default_timer()
        pool = ThreadPool(min(threads, len(arguments)))
        results = pool.map(star_download_station, arguments)
        pool.close()
        e_time = timeit.default_timer()

        if isinstance(results[0], list):
            results = itertools.chain.from_iterable(results)
        results = [_i for _i in results if _i is not None]

        filecount = 0
        download_size = 0
        # Update the station structures.
        for s_id, filename in results:
            filecount += 1
            station = self.stations[s_id]
            size = os.path.getsize(filename)
            download_size += size
            info = utils.get_stationxml_contents(filename)

            still_missing = {}
            # Make sure all missing information has been downloaded!
            for c_id, times in station.miss_station_information.items():
                # Get the temporal range of information in the file.
                c_info = [_i for _i in info if
                          _i.network == station.network and
                          _i.station == station.station and
                          _i.location == c_id[0] and
                          _i.channel == c_id[1]]
                if not c_info:
                    break
                starttime = min([_i.starttime for _i in c_info])
                endtime = max([_i.endtime for _i in c_info])
                if starttime > times[0] or endtime < times[1]:
                    still_missing[c_id] = times
                    continue
                station.have_station_information[c_id] = times
            station.miss_station_information = still_missing

        self.logger.info("Client '%s' - Downloaded %i station files [%.1f MB] "
                         "in %.1f seconds [%.2f KB/sec]." % (
                             self.client_name, filecount,
                             download_size / 1024.0 ** 2,
                             e_time - s_time,
                             (download_size / 1024.0) / (e_time - s_time)))

    def download_mseed(self, chunk_size_in_mb=25, threads_per_client=5):
        """
        Actually download MiniSEED data.

        :param chunk_size_in_mb:
        :param threads_per_client:
        :return:
        """
        # Estimate the download size to have equally sized chunks.
        channel_sampling_rate = {
            "F": 5000, "G": 5000, "D": 1000, "C": 1000, "E": 250, "S": 80,
            "H": 250, "B": 80, "M": 10, "L": 1, "V": 0.1, "U": 0.01,
            "R": 0.001, "P": 0.0001, "T": 0.00001, "Q": 0.000001, "A": 5000,
            "O": 5000}

        # Split into chunks of about equal size in terms of filesize.
        chunks = []
        chunks_curr = []
        curr_chunks_mb = 0

        counter = collections.Counter()

        # Keep track of attempted downloads.
        for sta in self.stations.values():
            for cha in sta.channels:
                # The band code is used to estimate the sampling rate of the
                # data to be downloaded.
                band_code = cha.channel[0].upper()
                try:
                    sr = channel_sampling_rate[band_code]
                except KeyError:
                    # Generic sampling rate for exotic band codes.
                    sr = 1.0

                for interval in cha.intervals:
                    counter[interval.status] += 1
                    # Only take those time intervals that actually require
                    # some downloading.
                    if interval.status != STATUS.NEEDS_DOWNLOADING:
                        continue
                    chunks_curr.append((
                        sta.network, sta.station, cha.location, cha.channel,
                        interval.start, interval.end, interval.filename))
                    # Assume that each sample needs 4 byte, STEIM
                    # compression reduces size to about a third.
                    # chunk size is in MB
                    duration = interval.end - interval.start
                    curr_chunks_mb += \
                        sr * duration * 4.0 / 3.0 / 1024.0 / 1024.0
                    if curr_chunks_mb >= chunk_size_in_mb:
                        chunks.append(chunks_curr)
                        chunks_curr = []
                        curr_chunks_mb = 0
        if chunks_curr:
            chunks.append(chunks_curr)

        keys = sorted(counter.keys())
        for key in keys:
            self.logger.info(
                "Client '%s' - Status for %i time intervals/channels before "
                "downloading: %s" % (self.client_name, counter[key],
                                     key.upper()))

        if not chunks:
            return []

        def star_download_mseed(args):
            """
            Star maps the arguments to the
            utils.download_and_split_mseed_bulk() function.

            :param args: The arguments to be passed.
            """
            try:
                ret_val = utils.download_and_split_mseed_bulk(
                    *args, logger=self.logger)
            except utils.ERRORS as e:
                msg = ("Client '%s' - " % args[1]) + str(e)
                if "no data available" in msg.lower():
                    self.logger.info(msg)
                else:
                    self.logger.error(msg)
                return []
            return ret_val

        pool = ThreadPool(min(threads_per_client, len(chunks)))

        d_start = timeit.default_timer()
        pool.map(
            star_download_mseed,
            [(self.client, self.client_name, chunk) for chunk in chunks])
        pool.close()
        d_end = timeit.default_timer()

        self.logger.info("Client '%s' - Launching basic QC checks..." %
                         self.client_name)
        downloaded_bytes, discarded_bytes = self._check_downloaded_data()
        total_bytes = downloaded_bytes + discarded_bytes

        self.logger.info("Client '%s' - Downloaded %.1f MB [%.2f KB/sec] of "
                         "data, %.1f MB of which were discarded afterwards." %
                         (self.client_name, total_bytes / 1024.0 ** 2,
                          total_bytes / 1024.0 / (d_end - d_start),
                          discarded_bytes / 1024.0 ** 2))

        # Recount everything to be able to emit some nice statistics.
        counter = collections.Counter()
        for sta in self.stations.values():
            for chan in sta.channels:
                for interval in chan.intervals:
                    counter[interval.status] += 1
        keys = sorted(counter.keys())
        for key in keys:
            self.logger.info(
                "Client '%s' - Status for %i time intervals/channels after "
                "downloading: %s" % (
                    self.client_name, counter[key], key.upper()))

    def sanitize_downloads(self):
        """
        Should be run after the MiniSEED and StationXML downloads finished.
        It will make sure that every MiniSEED file also has a corresponding
        StationXML file.
        """
        for station in self.stations.values():
            station.sanitize_downloads(logger=self.logger)

    def _check_downloaded_data(self):
        """
        Read the downloaded data, set the proper status flags and a remove
        data that does not meet the QC criteria. It just checks the
        downloaded data for minimum length and gaps/overlaps.

        Returns the downloaded_bytes and the discarded_bytes.
        """
        downloaded_bytes = 0
        discarded_bytes = 0
        for sta in self.stations.values():
            for cha in sta.channels:
                for interval in cha.intervals:
                    # The status of the intervals did not change!
                    if interval.status != STATUS.NEEDS_DOWNLOADING:
                        continue

                    # If the file does not exist, mark the time interval as
                    # download failed.
                    if not os.path.exists(interval.filename):
                        interval.status = STATUS.DOWNLOAD_FAILED
                        continue

                    size = os.path.getsize(interval.filename)
                    if size == 0:
                        self.logger.warning("Zero byte file '%s'. Will be "
                                            "deleted." % interval.filename)
                        utils.safe_delete(interval.filename)
                        interval.status = STATUS.DOWNLOAD_FAILED
                        continue

                    # Guard against faulty files.
                    try:
                        st = obspy.read(interval.filename, headonly=True)
                    except Exception as e:
                        self.logger.warning(
                            "Could not read file '%s' due to: %s\n"
                            "Will be discarded." % (interval.filename, str(e)))
                        utils.safe_delete(interval.filename)
                        discarded_bytes += size
                        interval.status = STATUS.DOWNLOAD_FAILED
                        continue

                    # Valid files with no data.
                    if len(st) == 0:
                        self.logger.warning(
                            "Empty file '%s'. Will be deleted." %
                            interval.filename)
                        utils.safe_delete(interval.filename)
                        discarded_bytes += size
                        interval.status = STATUS.DOWNLOAD_FAILED
                        continue

                    # If user did not want gappy files, remove them.
                    if self.restrictions.reject_channels_with_gaps is True and\
                            len(st) > 1:
                        self.logger.info("File '%s' contains %i traces. Will "
                                         "be deleted." % (interval.filename,
                                                          len(str)))
                        utils.safe_delete(interval.filename)
                        discarded_bytes += size
                        interval.status = STATUS.DOWNLOAD_REJECTED
                        continue

                    if self.restrictions.minimum_length:
                        duration = sum([tr.stats.endtime - tr.stats.starttime
                                        for tr in st])
                        expected_min_duration = \
                            self.restrictions.minimum_length * \
                            (interval.end - interval.start)
                        if duration < expected_min_duration:
                            self.logger.info(
                                "File '%s' has only %.2f seconds of data. "
                                "%.2f ar required. File will be deleted." %
                                (interval.filename, duration,
                                 expected_min_duration))
                        utils.safe_delete(interval.filename)
                        discarded_bytes += size
                        interval.status = STATUS.DOWNLOAD_REJECTED
                        continue

                    downloaded_bytes += size
                    interval.status = STATUS.DOWNLOADED
        return downloaded_bytes, discarded_bytes

    def _parse_miniseed_filenames(self, filenames, restrictions):
        time_range = restrictions.minimum_length * (restrictions.endtime -
                                                    restrictions.starttime)
        channel_availability = []
        for filename in filenames:
            st = obspy.read(filename, format="MSEED", headonly=True)
            if restrictions.reject_channels_with_gaps and len(st) > 1:
                self.logger.warning("Channel %s has gap or overlap. Will be "
                                    "removed." % st[0].id)
                try:
                    os.remove(filename)
                except OSError:
                    pass
                continue
            elif len(st) == 0:
                self.logger.error("MiniSEED file with no data detected. "
                                  "Should not happen!")
                continue
            tr = st[0]
            duration = tr.stats.endtime - tr.stats.starttime
            if restrictions.minimum_length and duration < time_range:
                self.logger.warning("Channel %s does not satisfy the minimum "
                                    "length requirement. %.2f seconds instead "
                                    "of the required %.2f seconds." % (
                                        tr.id, duration, time_range))
                try:
                    os.remove(filename)
                except OSError:
                    pass
                continue
            channel_availability.append(utils.ChannelAvailability(
                tr.stats.network, tr.stats.station, tr.stats.location,
                tr.stats.channel, tr.stats.starttime, tr.stats.endtime,
                filename))
        return channel_availability

    def discard_stations(self, station_ids):
        """
        Discard all stations based on the ids in station_ids.

        :param station_ids: An iterable yielding (NET, STA) tuples. All of
            these will be removed if available.
        """
        for station_id in station_ids:
            try:
                del self.stations[station_id]
            except KeyError:
                pass

    def get_availability(self):
        """
        Queries the current client for information of what stations are
        available given the spatial and temporal restrictions.
        """
        # Check if stations needs to be filtered after downloading or if the
        # restrictions one can impose with the FDSN webservices queries are
        # enough. This depends on the domain definition.
        try:
            self.domain.is_in_domain(0, 0)
            needs_filtering = True
        except NotImplementedError:
            needs_filtering = False

        arguments = {
            "network": self.restrictions.network,
            "station": self.restrictions.station,
            "location": self.restrictions.location,
            "channel": self.restrictions.channel,
            "starttime": self.restrictions.starttime,
            "endtime": self.restrictions.endtime,
            # Request at the channel level.
            "level": "channel"
        }
        # Add the domain specific query parameters.
        arguments.update(self.domain.get_query_parameters())

        # Check the capabilities of the service and see what is the most
        # appropriate way of acquiring availability information. Some services
        # right now require manual overwriting of what they claim to be
        # capable of.
        if self.client_name.lower() in OVERWRITE_CAPABILITIES:
            cap = OVERWRITE_CAPABILITIES[self.client_name.lower()]
            if cap is None:
                self.is_availability_reliable = False
            elif cap == "matchtimeseries":
                self.is_availability_reliable = True
                arguments["matchtimeseries"] = True
            elif cap == "includeavailability":
                self.is_availability_reliable = True
                arguments["includeavailability"] = True
            else:
                raise NotImplementedError
        elif "matchtimeseries" in self.client.services["station"]:
            arguments["matchtimeseries"] = True
            self.is_availability_reliable = True
        elif "includeavailability" in self.client.services["station"]:
            self.is_availability_reliable = True
            arguments["includeavailability"] = True
        else:
            self.is_availability_reliable = False

        if self.is_availability_reliable:
            self.logger.info("Client '%s' - Requesting reliable "
                             "availability." % self.client_name)
        else:
            self.logger.info(
                "Client '%s' - Requesting unreliable availability." %
                self.client_name)

        try:
            start = time.time()
            inv = self.client.get_stations(**arguments)
            end = time.time()
        except utils.ERRORS as e:
            if "no data available" in str(e).lower():
                self.logger.info(
                    "Client '%s' - No data available for request." %
                    self.client_name)
                return
            self.logger.error(
                "Client '{0}' - Failed getting availability: %s".format(
                    self.client_name), str(e))
            return
        self.logger.info("Client '%s' - Successfully requested availability "
                         "(%.2f seconds)" % (self.client_name, end - start))

        # Get the time intervals from the restrictions.
        intervals = [TimeInterval(start=_i[0], end=_i[1])
                     for _i in self.restrictions]
        for network in inv:
            for station in network:
                # Skip the station if it is not in the desired domain.
                if needs_filtering is True and \
                        not self.domain.is_in_domain(station.latitude,
                                                     station.longitude):
                    continue

                channels = []
                for channel in station.channels:
                    # Remove channels that somehow slipped past the temporal
                    # constraints due to weird behaviour from the data center.
                    if (channel.start_date > self.restrictions.starttime) or \
                            (channel.end_date < self.restrictions.endtime):
                        continue
                    # Use availability information if possible. In the other
                    # cases it should already work.
                    if "includeavailability" in arguments and \
                            arguments["includeavailability"]:
                        da = channel.data_availability
                        if da is None:
                            self.logger.warning(
                                "Client '%s' supports the "
                                "'includeavailability' parameter but returns "
                                "channels without availability information. "
                                "The final availability might not be "
                                "complete" % self.client_name)
                            continue
                        if (da.start > self.restrictions.starttime) or \
                                (da.end < self.restrictions.endtime):
                            continue
                    channels.append(Channel(
                        location=channel.location_code, channel=channel.code,
                        intervals=copy.deepcopy(intervals)))

                # Group by locations and apply the channel priority filter to
                # each.
                filtered_channels = []
                get_loc = lambda x: x.location
                for location, _channels in itertools.groupby(
                        sorted(channels, key=get_loc), get_loc):
                    filtered_channels.extend(utils.filter_channel_priority(
                        list(_channels), key="channel",
                        priorities=self.restrictions.channel_priorities))
                channels = filtered_channels

                # Filter to remove unwanted locations according to the priority
                # list.
                channels = utils.filter_channel_priority(
                    channels, key="location",
                    priorities=self.restrictions.location_priorities)

                if not channels:
                    continue

                self.stations[(network.code, station.code)] = Station(
                    network=network.code,
                    station=station.code,
                    latitude=station.latitude,
                    longitude=station.longitude,
                    channels=channels)
        self.logger.info("Client '%s' - Found %i station (%i channels)." % (
            self.client_name, len(self.stations),
            sum([len(_i.channels) for _i in self.stations.values()])))
