import copy
import collections

from configman import Namespace
from configman.converters import classes_in_namespaces_converter
from configman.dotdict import DotDict as ConfigmanDotDict

from socorro.external.boto.crashstorage import BotoS3CrashStorage, TelemetryBotoS3CrashStorage
from socorro.external.crashstorage_base import (
    CrashStorageBase,
    PolyStorageError,
    socorrodotdict_to_dict,
)
from socorro.external.es.crashstorage import ESCrashStorageRedactedJsonDump
from socorro.external.postgresql.crashstorage import PostgreSQLCrashStorage
from socorro.external.statsd.crashstorage import StatsdCrashStorage
from socorro.lib.util import DotDict as SocorroDotDict


CrashStorageConfig = collections.namedtuple('CrashStorageConfig', [
    'name',
    'storage_class',
    'config',
])


DEFAULT_STORAGE_CONFIGS = [
    CrashStorageConfig(
        name='PostgreSQL',
        storage_class=PostgreSQLCrashStorage,
        config={
            'benchmark_tag': 'PGBenchmarkWrite',
            'crashstorage_class': 'socorro.external.statsd.statsd_base.StatsdBenchmarkingWrapper',
            'statsd_prefix': 'processor.postgres',
            'transaction_executor_class': (
                'socorro.database.transaction_executor.TransactionExecutorWithInfiniteBackoff'
            ),
            'wrapped_crashstore': 'socorro.external.postgresql.crashstorage.PostgreSQLCrashStorage',
            'wrapped_object_class': (
                'socorro.external.postgresql.crashstorage.PostgreSQLCrashStorage'
            ),
        },
    ),
    CrashStorageConfig(
        name='BotoS3',
        storage_class=BotoS3CrashStorage,
        config={
            'active_list': 'save_raw_and_processed',
            'benchmark_tag': 'BotoBenchmarkWrite',
            'crashstorage_class': 'socorro.external.statsd.statsd_base.StatsdBenchmarkingWrapper',
            'statsd_prefix': 'processor.s3',
            'use_mapping_file': False,
            'wrapped_crashstore': 'socorro.external.boto.crashstorage.BotoS3CrashStorage',
            'wrapped_object_class': 'socorro.external.boto.crashstorage.BotoS3CrashStorage',
        },
    ),
    CrashStorageConfig(
        name='ElasticSearch',
        storage_class=ESCrashStorageRedactedJsonDump,
        config={
            'active_list': 'save_raw_and_processed',
            'benchmark_tag': 'BotoBenchmarkWrite',
            'crashstorage_class': 'socorro.external.statsd.statsd_base.StatsdBenchmarkingWrapper',
            'es_redactor.forbidden_keys': (
                'memory_report, upload_file_minidump_browser.json_dump, '
                'upload_file_minidump_flash1.json_dump, upload_file_minidump_flash2.json_dump'
            ),
            'statsd_prefix': 'processor.es',
            'use_mapping_file': False,
            'wrapped_crashstore': 'socorro.external.boto.crashstorage.BotoS3CrashStorage',
            'wrapped_object_class': (
                'socorro.external.es.crashstorage.ESCrashStorageRedactedJsonDump'
            ),
        },
    ),
    CrashStorageConfig(
        name='Statsd',
        storage_class=StatsdCrashStorage,
        config={
            'active_list': 'save_raw_and_processed',
            'crashstorage_class': 'socorro.external.statsd.statsd_base.StatsdCounter',
            'statsd_prefix': 'processor',
        },
    ),
    CrashStorageConfig(
        name='TelemetryBotoS3',
        storage_class=TelemetryBotoS3CrashStorage,
        config={
            'active_list': 'save_raw_and_processed',
            'bucket_name': 'org-mozilla-telemetry-crashes',
            'crashstorage_class': 'socorro.external.statsd.statsd_base.StatsdBenchmarkingWrapper',
            'statsd_prefix': 'processor.telemetry',
            'wrapped_object_class': (
                'socorro.external.boto.crashstorage.TelemetryBotoS3CrashStorage'
            ),
        },
    ),
]


class PolyCrashStorage(CrashStorageBase):
    """a crashstorage implementation that encapsulates a collection of other
    crashstorage instances.  Any save operation applied to an instance of this
    class will be applied to all the crashstorge in the collection.

    This class is useful for 'save' operations only.  It does not implement
    the 'get' operations.

    The contained crashstorage instances are specified in the configuration.
    Each class specified in the 'storage_classes' config option will be given
    its own numbered namespace in the form 'storage%d'.  With in the namespace,
    the class itself will be referred to as just 'store'.  Any configuration
    requirements within the class 'store' will be isolated within the local
    namespace.  That allows multiple instances of the same storageclass to
    avoid name collisions.
    """
    required_config = Namespace()
    required_config.add_option(
        'storage_classes',
        doc='a comma delimited list of storage classes',
        default='',
        from_string_converter=classes_in_namespaces_converter(
            template_for_namespace='storage%d',
            name_of_class_option='crashstorage_class',
            # we instantiate manually for thread safety
            instantiate_classes=False,
        ),
        likely_to_be_changed=True,
    )

    def __init__(self, config, storage_configs=None, quit_check_callback=None):
        """instantiate all the subordinate crashstorage instances

        parameters:
            config - a configman dot dict holding configuration information
            quit_check_callback - a function to be called periodically during
                                  long running operations.

        instance variables:
            self.storage_namespaces - the list of the namespaces inwhich the
                                      subordinate instances are stored.
            self.stores - instances of the subordinate crash stores

        """
        super(PolyCrashStorage, self).__init__(config, quit_check_callback)

        storage_configs = storage_configs or DEFAULT_STORAGE_CONFIGS

        self.stores = ConfigmanDotDict()
        for storage_config in storage_configs:
            self.stores[storage_config.name] = storage_config.storage_class(
                config,
                quit_check_callback
            )

    def close(self):
        """iterate through the subordinate crash stores and close them.
        Even though the classes are closed in sequential order, all are
        assured to close even if an earlier one raises an exception.  When all
        are closed, any exceptions that were raised are reraised in a
        PolyStorageError

        raises:
          PolyStorageError - an exception container holding a list of the
                             exceptions raised by the subordinate storage
                             systems"""
        storage_exception = PolyStorageError()
        for a_store in self.stores.itervalues():
            try:
                a_store.close()
            except Exception as x:
                self.logger.error('%s failure: %s', a_store.__class__,
                                  str(x))
                storage_exception.gather_current_exception()
        if storage_exception.has_exceptions():
            raise storage_exception

    def save_raw_crash(self, raw_crash, dumps, crash_id):
        """iterate through the subordinate crash stores saving the raw_crash
        and the dump to each of them.

        parameters:
            raw_crash - the meta data mapping
            dumps - a mapping of dump name keys to dump binary values
            crash_id - the id of the crash to use"""
        storage_exception = PolyStorageError()
        for a_store in self.stores.itervalues():
            self.quit_check()
            try:
                a_store.save_raw_crash(raw_crash, dumps, crash_id)
            except Exception as x:
                self.logger.error('%s failure: %s', a_store.__class__,
                                  str(x))
                storage_exception.gather_current_exception()
        if storage_exception.has_exceptions():
            raise storage_exception

    def save_processed(self, processed_crash):
        """iterate through the subordinate crash stores saving the
        processed_crash to each of the.

        parameters:
            processed_crash - a mapping containing the processed crash"""
        storage_exception = PolyStorageError()
        for a_store in self.stores.itervalues():
            self.quit_check()
            try:
                a_store.save_processed(processed_crash)
            except Exception as x:
                self.logger.error('%s failure: %s', a_store.__class__,
                                  str(x), exc_info=True)
                storage_exception.gather_current_exception()
        if storage_exception.has_exceptions():
            raise storage_exception

    def save_raw_and_processed(self, raw_crash, dump, processed_crash,
                               crash_id):
        storage_exception = PolyStorageError()

        # Later we're going to need to clone this per every crash storage
        # in the loop. But, to save time, before we do that, convert the
        # processed crash which is a SocorroDotDict into a pure python
        # dict which we can more easily copy.deepcopy() operate on.
        processed_crash_as_dict = socorrodotdict_to_dict(processed_crash)
        raw_crash_as_dict = socorrodotdict_to_dict(raw_crash)

        for a_store in self.stores.itervalues():
            self.quit_check()
            try:
                actual_store = getattr(a_store, 'wrapped_object', a_store)

                if hasattr(actual_store, 'is_mutator') and actual_store.is_mutator():
                    # We do this because `a_store.save_raw_and_processed`
                    # expects the processed crash to be a DotDict but
                    # you can't deepcopy those, so we deepcopy the
                    # pure dict version and then dress it back up as a
                    # DotDict.
                    my_processed_crash = SocorroDotDict(
                        copy.deepcopy(processed_crash_as_dict)
                    )
                    my_raw_crash = SocorroDotDict(
                        copy.deepcopy(raw_crash_as_dict)
                    )
                else:
                    my_processed_crash = processed_crash
                    my_raw_crash = raw_crash

                a_store.save_raw_and_processed(
                    my_raw_crash,
                    dump,
                    my_processed_crash,
                    crash_id
                )
            except Exception:
                store_class = getattr(
                    a_store, 'wrapped_object', a_store.__class__
                )
                self.logger.error(
                    '%r failed (crash id: %s)',
                    store_class,
                    crash_id,
                    exc_info=True
                )
                storage_exception.gather_current_exception()
        if storage_exception.has_exceptions():
            raise storage_exception
