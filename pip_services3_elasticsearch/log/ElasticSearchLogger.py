# -*- coding: utf-8 -*-

from datetime import datetime, timezone

from elasticsearch import Elasticsearch
from moment import Moment
from pip_services3_commons.data import IdGenerator
from pip_services3_commons.errors import ConfigException
from pip_services3_commons.refer import IReferenceable
from pip_services3_commons.run import IOpenable
from pip_services3_components.log import CachedLogger
from pip_services3_components.test.SetInterval import SetInterval
from pip_services3_rpc.connect import HttpConnectionResolver


class ElasticSearchLogger(CachedLogger, IReferenceable, IOpenable):
    """
    Logger that dumps execution logs to ElasticSearch service.

    ElasticSearch is a popular search index. It is often used
    to store and index execution logs by itself or as a part of
    ELK (ElasticSearch - Logstash - Kibana) stack.

    Authentication is not supported in this version.

    ### Configuration parameters ###
        - level:             maximum log level to capture
        - source:            source (context) name
        - connection(s):
            - discovery_key:         (optional) a key to retrieve the connection from :class:`IDiscovery <pip_services3_components.connect.IDiscovery.IDiscovery>`
            - protocol:              connection protocol: http or https
            - host:                  host name or IP address
            - port:                  port number
            - uri:                   resource URI or connection string with all parameters in it
        - options:
            - interval:        interval in milliseconds to save log messages (default: 10 seconds)
            - max_cache_size:  maximum number of messages stored in this cache (default: 100)
            - index:           ElasticSearch index name (default: "log")
            - date_format      The date format to use when creating the index name. Eg. log-YYYYMMDD (default: "YYYYMMDD").
            - daily:           True to create a new index every day by adding date suffix to the index name (default: False)
            - reconnect:       reconnect timeout in milliseconds (default: 60 sec)
            - timeout:         invocation timeout in milliseconds (default: 30 sec)
            - max_retries:     maximum number of retries (default: 3)
            - index_message:   True to enable indexing for message object (default: False)

    ### References ###
        - *:context-info:*:*:1.0    (optional) :class:`ContextInfo <pip_services3_components.info.ContextInfo.ContextInfo>` to detect the context id and specify counters source
        - *:discovery:*:*:1.0       (optional) :class:`IDiscovery <pip_services3_components.connect.IDiscovery.IDiscovery>` services to resolve connection

    ### Example ###

    .. code-block:: python

        let logger = new ElasticSearchLogger();
        logger.configure(ConfigParams.fromTuples(
            "connection.protocol", "http",
            "connection.host", "localhost",
            "connection.port", 9200
        ));

        try:
            logger.open("123")
        except Exception as err:
            # do something

        logger.error("123", ex, "Error occured: {}", ex.message);
        logger.debug("123", "Everything is OK.");
    """

    def __init__(self):
        """
        Creates a new instance of the logger.
        """
        super(ElasticSearchLogger, self).__init__()

        self.__connection_resolver = HttpConnectionResolver()

        self.__timer = None
        self.__index = 'log'
        self._date_format = 'YYYYMMDD'
        self.__daily_index = False
        self.__current_index = ''
        self.__reconnect = 60000
        self.__timeout = 30000
        self.__max_retries = 3
        self.__index_message = False

        self.__client = None

    def configure(self, config):
        """
        Configures component by passing configuration parameters.

        :param config: configuration parameters to be set.
        """
        super().configure(config)

        self.__connection_resolver.configure(config)

        self.__index = config.get_as_string_with_default('index', self.__index)
        self._date_format = config.get_as_string_with_default('date_format', self._date_format)
        self.__daily_index = config.get_as_string_with_default('daily', self.__daily_index)
        self.__reconnect = config.get_as_string_with_default('options.reconnect', self.__reconnect)
        self.__timeout = config.get_as_string_with_default('options.timeout', self.__timeout)
        self.__max_retries = config.get_as_string_with_default('options.max_retries', self.__max_retries)
        self.__index_message = config.get_as_string_with_default('options.index_message', self.__index_message)

    def set_references(self, references):
        """
        Sets references to dependent components.

        :param references: references to locate the component dependencies.
        """
        super().set_references(references)
        self.__connection_resolver.set_references(references)

    def is_open(self):
        """
        Checks if the component is opened.

        :return: True if the component has been opened and False otherwise.
        """
        return self.__timer is not None

    def open(self, correlation_id):
        """
        Opens the component.

        :param correlation_id: (optional) transaction id to trace execution through call chain.
        """
        if self.is_open():
            return

        connection = self.__connection_resolver.resolve(correlation_id)
        if connection is None:
            raise ConfigException(correlation_id, 'NO_CONNECTION', 'Connection is not configured')
        uri = connection.get_uri()

        options = {
            'request_timeout': self.__timeout,
            'dead_timeout': self.__reconnect,
            'max_retries': self.__max_retries
        }

        self.__client = Elasticsearch(hosts=[uri], kwargs=options)
        try:
            self.__create_index_if_needed(correlation_id, True)
            self.__timer = SetInterval(self.dump, self._interval)
            self.__timer.start()
        except Exception as err:
            raise err

    def close(self, correlation_id):
        """
        Closes component and frees used resources.

        :param correlation_id: (optional) transaction id to trace execution through call chain.
        """
        try:
            self._save(self._cache)
            if self.__timer:
                self.__timer.stop()
            self._cache = []
            self.__timer = None
            self.__client = None

        except Exception as err:
            raise err

    def __get_current_index(self):
        if not self.__daily_index: return self.__index

        today = datetime.utcnow().astimezone(tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        date_pattern = Moment(today).format(self._date_format)

        return self.__index + '-' + date_pattern

    def __create_index_if_needed(self, correlation_id, force):
        new_index = self.__get_current_index()
        if not force and self.__current_index == new_index:
            return

        self.__current_index = new_index

        try:
            if self.__client.indices.exists(index=self.__current_index):
                self.__client.indices.create(index=self.__current_index, body={
                    'settings': {'number_of_shards': 1},
                    'mappings': {
                        'log_message': {
                            'properties': {
                                'time': {'type': 'date', 'index': True},
                                'source': {'type': "keyword", 'index': True},
                                'level': {'type': "keyword", 'index': True},
                                'correlation_id': {'type': "text", 'index': True},
                                'error': {
                                    'type': 'object',
                                    'properties': {
                                        'type': {'type': "keyword", 'index': True},
                                        'category': {'type': "keyword", 'index': True},
                                        'status': {'type': "integer", 'index': False},
                                        'code': {'type': "keyword", 'index': True},
                                        'message': {'type': "text", 'index': False},
                                        'details': {'type': "object"},
                                        'correlation_id': {'type': "text", 'index': False},
                                        'cause': {'type': "text", 'index': False},
                                        'stack_trace': {'type': "text", 'index': False}
                                    }
                                },
                                'message': {'type': 'text', 'index': self.__index_message}
                            }
                        }
                    }
                })
        except Exception as err:
            # Skip already exist errors
            if 'resource_already_exists' in str(err):
                return
            raise err

    def _save(self, messages):
        """
        Saves log messages from the cache.

        :param messages:  a list with log messages
        """
        if not self.is_open() and len(messages) == 0:
            return
        try:
            self.__create_index_if_needed('elasticsearch_logger', False)
            bulk = []
            for message in messages:
                bulk.append({
                    'index': {
                        '_index': self.__current_index,
                        '_type': 'log_message',
                        '_id': IdGenerator.next_long()
                    }
                })

                # Convert objects for json serialization
                # TODO: Maybe need move this to other module
                if hasattr(message, 'error') and message.error is not None:
                    message.error = message.error.__dict__
                message.time = str(message.time)

                bulk.append(message.__dict__)

            if bulk:
                self.__client.bulk(body=bulk)

        except Exception as err:
            raise err