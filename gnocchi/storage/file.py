# -*- encoding: utf-8 -*-
#
# Copyright © 2014 Objectif Libre
# Copyright © 2015-2018 Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import collections
import errno
import itertools
import operator
import os
import shutil
import tempfile

from oslo_config import cfg
import six

from gnocchi import carbonara
from gnocchi import storage
from gnocchi import utils


OPTS = [
    cfg.StrOpt('file_basepath',
               default='/var/lib/gnocchi',
               help='Path used to store gnocchi data files.'),
]

ATTRGETTER_METHOD = operator.attrgetter("method")

# Python 2 compatibility
try:
    FileNotFoundError
except NameError:
    FileNotFoundError = None


class FileStorage(storage.StorageDriver):
    WRITE_FULL = True

    def __init__(self, conf):
        super(FileStorage, self).__init__(conf)
        self.basepath = conf.file_basepath
        self.basepath_tmp = os.path.join(self.basepath, 'tmp')

    def upgrade(self):
        utils.ensure_paths([self.basepath_tmp])

    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, str(self.basepath))

    def _atomic_file_store(self, dest, data):
        tmpfile = tempfile.NamedTemporaryFile(
            prefix='gnocchi', dir=self.basepath_tmp,
            delete=False)
        tmpfile.write(data)
        tmpfile.close()
        os.rename(tmpfile.name, dest)

    def _build_metric_dir(self, metric):
        return os.path.join(self.basepath, str(metric.id))

    def _build_unaggregated_timeserie_path(self, metric, version=3):
        return os.path.join(
            self._build_metric_dir(metric),
            'none' + ("_v%s" % version if version else ""))

    def _build_metric_path(self, metric, aggregation):
        return os.path.join(self._build_metric_dir(metric),
                            "agg_" + aggregation)

    def _build_metric_path_for_split(self, metric, aggregation,
                                     key, version=3):
        path = os.path.join(
            self._build_metric_path(metric, aggregation),
            str(key)
            + "_"
            + str(utils.timespan_total_seconds(key.sampling)))
        return path + '_v%s' % version if version else path

    def _create_metric(self, metric):
        path = self._build_metric_dir(metric)
        try:
            os.mkdir(path, 0o750)
        except OSError as e:
            if e.errno == errno.EEXIST:
                raise storage.MetricAlreadyExists(metric)
            raise
        for agg in metric.archive_policy.aggregation_methods:
            try:
                os.mkdir(self._build_metric_path(metric, agg), 0o750)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise

    def _store_unaggregated_timeseries_unbatched(
            self, metric, data, version=3):
        dest = self._build_unaggregated_timeserie_path(metric, version)
        with open(dest, "wb") as f:
            f.write(data)

    def _get_or_create_unaggregated_timeseries_unbatched(
            self, metric, version=3):
        path = self._build_unaggregated_timeserie_path(metric, version)
        try:
            with open(path, 'rb') as f:
                return f.read()
        except FileNotFoundError:
            pass
        except IOError as e:
            if e.errno != errno.ENOENT:
                raise
        try:
            self._create_metric(metric)
        except storage.MetricAlreadyExists:
            pass

    def _list_split_keys(self, metric, aggregations, version=3):
        keys = collections.defaultdict(set)
        for method, grouped_aggregations in itertools.groupby(
                sorted(aggregations, key=ATTRGETTER_METHOD),
                ATTRGETTER_METHOD):
            try:
                files = os.listdir(
                    self._build_metric_path(metric, method))
            except OSError as e:
                if e.errno == errno.ENOENT:
                    raise storage.MetricDoesNotExist(metric)
                raise
            raw_keys = list(map(
                lambda k: k.split("_"),
                filter(
                    lambda f: self._version_check(f, version),
                    files)))
            if not raw_keys:
                continue
            zipped = list(zip(*raw_keys))
            k_timestamps = utils.to_timestamps(zipped[0])
            k_granularities = list(map(utils.to_timespan, zipped[1]))
            grouped_aggregations = list(grouped_aggregations)
            for timestamp, granularity in six.moves.zip(
                    k_timestamps, k_granularities):
                for agg in grouped_aggregations:
                    if granularity == agg.granularity:
                        keys[agg].add(carbonara.SplitKey(
                            timestamp,
                            sampling=granularity))
                        break
        return keys

    def _delete_metric_splits_unbatched(
            self, metric, key, aggregation, version=3):
        os.unlink(self._build_metric_path_for_split(
            metric, aggregation, key, version))

    def _store_metric_splits(self, metric, keys_and_data_and_offset,
                             aggregation, version=3):
        for key, data, offset in keys_and_data_and_offset:
            self._atomic_file_store(
                self._build_metric_path_for_split(
                    metric, aggregation, key, version),
                data)

    def _delete_metric(self, metric):
        path = self._build_metric_dir(metric)
        try:
            shutil.rmtree(path)
        except OSError as e:
            if e.errno != errno.ENOENT:
                # NOTE(jd) Maybe the metric has never been created (no
                # measures)
                raise

    def _get_measures_unbatched(self, metric, key, aggregation, version=3):
        path = self._build_metric_path_for_split(
            metric, aggregation, key, version)
        try:
            with open(path, 'rb') as aggregation_file:
                return aggregation_file.read()
        except IOError as e:
            if e.errno == errno.ENOENT:
                if os.path.exists(self._build_metric_dir(metric)):
                    raise storage.AggregationDoesNotExist(
                        metric, aggregation, key.sampling)
                raise storage.MetricDoesNotExist(metric)
            raise
