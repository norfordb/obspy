#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Download helpers.

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

import logging
from multiprocessing.pool import ThreadPool
import warnings

import obspy
from obspy.core.util.obspy_types import OrderedDict
from obspy.fdsn.header import URL_MAPPINGS, FDSNException
from obspy.fdsn import Client

from . import utils
from .download_status import ClientDownloadHelper


# Setup the logger.
logger = logging.getLogger("obspy-download-helper")
logger.setLevel(logging.DEBUG)
# Prevent propagating to higher loggers.
logger.propagate = 0
# Console log handler.
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
# Add formatter
FORMAT = "[%(asctime)s] - %(name)s - %(levelname)s: %(message)s"
formatter = logging.Formatter(FORMAT)
ch.setFormatter(formatter)
logger.addHandler(ch)


class FDSNDownloadHelperException(FDSNException):
    pass


class Restrictions(object):
    """
    Class storing non-domain restrictions of a query. This is best explained
    with two examples. See the list below for a more detailed explanation
    of the parameters. The first set of restrictions is useful for event
    based earthquake set queries.

    >>> import obspy
    >>> restrictions = Restrictions(
    ...     # Get data from 5 minutes before the event to one hours after the
    ...     # event.
    ...     starttime=obspy.UTCDateTime(2012, 1, 1)
    ...     endtime=obspy.UTCDateTime(2012, 1, 2)
    ...     # You might not want to deal with gaps in the data.
    ...     reject_channels_with_gaps=True,
    ...     # And you might only want waveform that have data for at least
    ...     # 95 % of the requested time span.
    ...     minimum_length=0.95,
    ...     # No two stations should be closer than 10 km to each other.
    ...     minimum_interstation_distance_in_m=10E3,
    ...     # Only HH or BH channels. If a station has HH channels,
    ...     # those will be downloaded, otherwise the BH. Nothing will be
    ...     # downloaded if it has neither.
    ...     channel_priorities=("HH[Z,N,E]", "BH[Z,N,E]"),
    ...     # Locations codes are arbitrary and there is no rule which
    ...     # location is best.
    ...     location_priorities=("", "00", "10"))


    And the restrictions for downloading a noise data set might look similar to
    the following:

    >>> import obspy
    >>> restrictions = Restrictions(
    ...     # Get data for a whole year.
    ...     starttime=obspy.UTCDateTime(2012, 1, 1),
    ...     endtime=obspy.UTCDateTime(2013, 1, 1),
    ...     # Chunk it to have one file per day.
    ...     chunklength=86400,
    ...     # Considering the enormous amount of data associated with
    ...     # continuous requests, you might want to limit the data based on
    ...     # SEED identifiers. If the location code is specified, the
    ...     # location priority list is not used; the same is true for the
    ...     # channel argument and priority list.
    ...     network="BW", station="A*", location="", channel="BH*",
    ...     # The typical use case for such a data set are noise correlations
    ...     # where gaps are dealt with at a later stage.
    ...     reject_channels_with_gaps=False,
    ...     # Same is true with the minimum length. Any data during a day
    ...     # might be useful.
    ...     minimum_length=0.0,
    ...     # Guard against the same station having different names.
    ...     minimum_interstation_distance_in_m=100.0)


    :param starttime: The starttime of the data to be downloaded.
    :type starttime: :class:`~obspy.core.utcdatetime.UTCDateTime`
    :param endtime: The endtime of the data.
    :type endtime: :class:`~obspy.core.utcdatetime.UTCDateTime`
    :param chunklength_in_sec: The length of one chunk in seconds. If set,
        the time between ``starttime`` and ``endtime`` will be divided into
        segments of ``chunklength`` seconds. Useful for continuous data
        requests. Set to ``None`` if one piece of data is desired between
        ``starttime`` and ``endtime`` (the default).
    :type chunklength_in_sec: float, optional
    :param network: The network code. Can contain wildcards.
    :type network: str, optional
    :param station: The station code. Can contain wildcards.
    :type station: str, optional
    :param location: The location code. Can contain wildcards.
    :type location: str, optional
    :param channel: The channel code. Can contain wildcards.
    :type channel: str, optional
    :param reject_channels_with_gaps: If True (default), MiniSEED files with
        gaps and/or overlaps will be rejected.
    :type reject_channels_with_gaps: boolean, optional
    :param minimum_length: The minimum length of the data as a fraction of
        the requested time frame. After a channel has been downloaded it
        will be checked that its total length is at least that fraction of
        the requested time span. Will be rejected otherwise. Must be between
        ``0.0`` and ``1.0``, defaults to ``0.9``.
    :type minimum_length: float, optional
    :param minimum_interstation_distance_in_m: The minimum inter-station
        distance. Data from any new station closer to any existing station
        will not be downloaded. Also used for duplicate station detection as
        sometimes stations have different names for different webservice
        providers. Defaults to `1000 m`.
    :type minimum_interstation_distance_in_m: float, optional
    :param channel_priorities: Priority list for the channels. Will not be
        used if the ``channel`` argument is used.
    :type channel_priorities: list of str
    :param location_priorities: Priority list for the locations. Will not be
        used if the ``location`` argument is used.
    :type location_priorities: list of str
    """

    def __init__(self, starttime, endtime, chunklength_in_sec=None,
                 network=None, station=None, location=None, channel=None,
                 reject_channels_with_gaps=True, minimum_length=0.9,
                 minimum_interstation_distance_in_m=1000,
                 channel_priorities=("HH[Z,N,E]", "BH[Z,N,E]",
                                     "MH[Z,N,E]", "EH[Z,N,E]",
                                     "LH[Z,N,E]"),
                 location_priorities=("", "00", "10")):
        self.starttime = obspy.UTCDateTime(starttime)
        self.endtime = obspy.UTCDateTime(endtime)
        self.chunklength = float(chunklength_in_sec) if chunklength_in_sec is not None \
            else None
        self.network = network
        self.station = station
        self.location = location
        self.channel = channel
        self.reject_channels_with_gaps = reject_channels_with_gaps
        self.minimum_length = minimum_length
        self.channel_priorities = channel_priorities
        self.location_priorities = location_priorities
        self.minimum_interstation_distance_in_m = \
            float(minimum_interstation_distance_in_m)

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        return not self.__eq___(other)

    def __iter__(self):
        """
        Iterator yielding time intervals based on the chunklength and
        temporal settings.
        """
        if not self.chunklength:
            return iter([(self.starttime, self.endtime)])

        def it():
            starttime = self.starttime
            endtime = self.endtime
            chunklength = self.chunklength

            while starttime < endtime:
                yield (starttime, min(starttime + chunklength, endtime))
                starttime += chunklength
            raise StopIteration
        return it()


class DownloadHelper(object):
    """
    Class facilitating data acquistion across all FDSN web service
    implementation.

    :param providers: List of FDSN client names or service URLS. Will use
        all FDSN implementations known to ObsPy if set to None. The order
        in the list also determines their priority, if data is available at
        more then one provider it will always be downloaded from the
        provider that comes first in the list.
    """
    def __init__(self, providers=None):
        if providers is None:
            providers = dict(URL_MAPPINGS.items())
            # In that case make sure IRIS is first, and ORFEUS second! The
            # remaining items will be sorted alphabetically to make it
            # deterministic at least to a certain extent.
            _p = []
            if "IRIS" in providers:
                _p.append("IRIS")
                del providers["IRIS"]
            if "ORFEUS" in providers:
                _p.append("ORFEUS")
                del providers["ORFEUS"]
            _p.extend(sorted(providers))
            providers = _p

        self.providers = tuple(providers)

        # Initialize all clients.
        self._initialized_clients = OrderedDict()
        self.__initialize_clients()

    def download(self, domain, restrictions, mseed_storage,
                 stationxml_storage, download_chunk_size_in_mb=50,
                 threads_per_client=5):
        """
        Download data.

        :param domain:
        :param restrictions:
        :param mseed_storage:
        :param stationxml_storage:
        :param download_chunk_size_in_mb:
        :param threads_per_client:
        """
        # Collect all the downloaded stations.
        existing_stations = set()

        # Set of network and station tuples, e.g. {(“NET1”, “STA1”),
        # (“NET2”, “STA2”), …}. Will be used to not attempt to download
        # stations that have been rejected during a previous loop iteration.
        # Station can be rejected if they are too close to an already existing
        # station.
        discarded_station_ids = set()

        report = []

        # Do it sequentially for each client. Doing it in parallel is not
        # really feasible as long as the availability queries are not reliable.
        for client_name, client in self._initialized_clients.items():
            logger.info("Stations already acquired during this run: %i" %
                        len(existing_stations))

            # The client download helper object is responsible for the
            # downloads of a single FDSN endpoint.
            helper = ClientDownloadHelper(
                client=client, client_name=client_name,
                restrictions=restrictions, domain=domain,
                mseed_storage=mseed_storage,
                stationxml_storage=stationxml_storage, logger=logger)

            # Request the availability.
            helper.get_availability()
            # Continue if there is not data.
            if not helper:
                report.append({"client": client_name, "data": []})
                continue

            # First filter stage. Remove stations based on the station id,
            # e.g. NETWORK.STATION. Remove all that already exist and all
            # that are in the discarded station ids set.
            helper.discard_stations(existing_stations.union(
                discarded_station_ids))

            logger.info("Client '%s' - After discarding duplicates based on "
                        "the station id, %i stations remain." % (
                            client_name, len(helper)))
            # If nothing is there, no need to keep going.
            if not helper:
                report.append({"client": client_name, "data": []})
                continue

            # Filter based on the distance to the next closest station. If
            # info["reliable"] is True, it is assumed that we can actually
            # get all the data in the availability, otherwise everything
            # will be attempted to be downloaded.
            # f = utils.filter_based_on_interstation_distance(
            #     existing_stations=existing_stations,
            #     new_stations=availability,
            #     reliable_new_stations=info["reliable"],
            #     minimum_distance_in_m=
            #     restrictions.minimum_interstation_distance_in_m)
            # # Add the rejected stations to the set of discarded station ids
            # # so they will not be attempted to be downloaded again.
            # for station in f["rejected_stations"]:
            #     discarded_station_ids.add((station.network, station.station))
            # availability = f["accepted_stations"]
            #
            # logger.info("Client '%s' - %i station(s) satisfying the "
            #             "minimum inter-station distance found." % (
            #             client_name, len(availability)))
            # if not availability:
            #     report.append({"client": client_name, "data": []})
            #     continue

            # Download MiniSEED data.
            helper.prepare_mseed_download()
            helper.download_mseed(chunk_size_in_mb=download_chunk_size_in_mb,
                                  threads_per_client=threads_per_client)

            # Download StationXML data.
            helper.prepare_stationxml_download()
            helper.download_stationxml()

            # Sanitize the downloaded things. Assures that all waveform data
            # also has corresponding
            helper.sanitize_downloads()

        return report

    def __initialize_clients(self):
        """
        Initialize all clients.
        """
        logger.info("Initializing FDSN client(s) for %s."
                    % ", ".join(self.providers))

        def _get_client(client_name):
            try:
                this_client = Client(client_name)
            except utils.ERRORS as e:
                if "timeout" in str(e).lower():
                    extra = " (timeout)"
                else:
                    extra = ""
                logger.warn("Failed to initialize client '%s'.%s"
                            % (client_name, extra))
                return client_name, None
            services = sorted([_i for _i in this_client.services.keys()
                               if not _i.startswith("available")])
            if "dataselect" not in services or "station" not in services:
                logger.info("Cannot use client '%s' as it does not have "
                            "'dataselect' and/or 'station' services."
                            % client_name)
                return client_name, None
            return client_name, this_client

        # Catch warnings in the main thread. The catch_warnings() context
        # manager does not reliably work when used in multiple threads.
        p = ThreadPool(len(self.providers))
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            clients = p.map(_get_client, self.providers)
        p.close()
        for warning in w:
            logger.debug("Warning during initializing one of the clients: " +
                         str(warning.message))

        clients = {key: value for key, value in clients if value is not None}
        # Write to initialized clients dictionary preserving order.
        for client in self.providers:
            if client not in clients:
                continue
            self._initialized_clients[client] = clients[client]

        logger.info("Successfully initialized %i client(s): %s."
                    % (len(self._initialized_clients),
                       ", ".join(self._initialized_clients.keys())))