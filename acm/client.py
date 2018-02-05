import base64
import hashlib
import hmac
import logging
import socket
import time
import ssl

from multiprocessing import Process, Manager, Queue, pool
from threading import RLock, Thread

try:
    # python3.6
    from http import HTTPStatus
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode, unquote_plus
    from urllib.error import HTTPError, URLError
except ImportError:
    # python2.7
    import httplib as HTTPStatus
    from urllib2 import Request, urlopen, HTTPError, URLError
    from urllib import urlencode, unquote_plus

    base64.encodebytes = base64.encodestring

from .commons import synchronized_with_attr, truncate
from .params import group_key, parse_key, is_valid
from .server import get_server_list
from .files import read_file, save_file, delete_file

logger = logging.getLogger("acm")

DEBUG = False
VERSION = "0.1.8"

DEFAULT_GROUP_NAME = "DEFAULT_GROUP"
WORD_SEPARATOR = bytes([2]).decode()
LINE_SEPARATOR = bytes([1]).decode()

DEFAULTS = {
    "APP_NAME": "ACM-SDK-Python",
    "TIMEOUT": 3,  # in seconds
    "GROUP": "DEFAULT_GROUP",
    "NAMESPACE": "DEFAULT_TENANT",
    "PULLING_TIMEOUT": 30,  # in seconds
    "PULLING_CONFIG_SIZE": 3000,
    "CALLBACK_THREAD_NUM": 10,
    "FAILOVER_BASE": "acm-data/data",
    "SNAPSHOT_BASE": "acm-data/snapshot"
}

OPTIONS = {"default_timeout", "tls_enabled", "auth_enabled", "cai_enabled", "pulling_timeout", "pulling_config_size",
           "callback_thread_num", "failover_base", "snapshot_base", "app_name"}


class ACMException(Exception):
    pass


def process_common_params(data_id, group):
    if not group or not group.strip():
        group = DEFAULT_GROUP_NAME
    else:
        group = group.strip()

    if not data_id or not is_valid(data_id):
        raise ACMException("Invalid dataId.")

    if not is_valid(group):
        raise ACMException("Invalid group.")
    return data_id, group


def parse_pulling_result(result):
    if not result:
        return list()
    return [i.split(WORD_SEPARATOR) for i in unquote_plus(result.decode(), "utf8").split(LINE_SEPARATOR) if i.strip()]


class WatcherWrap:
    def __init__(self, key, callback):
        self.callback = callback
        self.last_md5 = None
        self.watch_key = key


class CacheData:
    def __init__(self, key, client):
        self.key = key
        local_value = read_file(client.failover_base, key) or read_file(client.snapshot_base, key)
        self.content = local_value
        self.md5 = hashlib.md5(local_value.encode()).hexdigest() if local_value else None
        self.is_init = True
        if not self.md5:
            logger.debug("[init-cache] cache for %s does not have local value" % key)


class ACMClient:
    """Client for ACM

    available API:
    * get
    * add_watcher
    * remove_watcher
    """
    debug = False

    @staticmethod
    def set_debugging():
        if not ACMClient.debug:
            global logger
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s:%(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            ACMClient.debug = True

    def __init__(self, endpoint, namespace=None, ak=None, sk=None, ):
        self.endpoint = endpoint
        self.namespace = namespace or DEFAULTS["NAMESPACE"]
        self.ak = ak
        self.sk = sk

        self.server_list = None
        self.server_list_lock = RLock()
        self.current_server = None
        self.server_offset = 0
        self.server_refresh_running = False

        self.watcher_mapping = dict()
        self.pulling_lock = RLock()
        self.puller_mapping = None
        self.notify_queue = None
        self.callback_tread_pool = None
        self.process_mgr = None

        self.default_timeout = DEFAULTS["TIMEOUT"]
        self.tls_enabled = False
        self.auth_enabled = self.ak and self.sk
        self.cai_enabled = True
        self.pulling_timeout = DEFAULTS["PULLING_TIMEOUT"]
        self.pulling_config_size = DEFAULTS["PULLING_CONFIG_SIZE"]
        self.callback_tread_num = DEFAULTS["CALLBACK_THREAD_NUM"]
        self.failover_base = DEFAULTS["FAILOVER_BASE"]
        self.snapshot_base = DEFAULTS["SNAPSHOT_BASE"]
        self.app_name = DEFAULTS["APP_NAME"]

        logger.info("[client-init] endpoint:%s, tenant:%s" % (endpoint, namespace))

    def set_options(self, **kwargs):
        for k, v in kwargs.items():
            if k not in OPTIONS:
                logger.warning("[set_options] unknown option:%s, ignored" % k)
                continue

            logger.debug("[set_options] key:%s, value:%s" % (k, v))
            self.__setattr__(k, v)

    def _refresh_server_list(self):
        with self.server_list_lock:
            if self.server_refresh_running:
                logger.warning("[refresh-server] task is running, aborting")
                return
            self.server_refresh_running = True

        while True:
            try:
                time.sleep(30)
                logger.debug("[refresh-server] try to refresh server list")
                server_list = get_server_list(self.endpoint, 443 if self.tls_enabled else 8080, self.cai_enabled)
                logger.debug(
                    "[refresh-server] server_num:%s server_list:%s" % (len(server_list), server_list))
                if not server_list:
                    logger.error("[refresh-server] empty server_list get from %s, do not refresh" % self.endpoint)
                    continue
                with self.server_list_lock:
                    self.server_list = server_list
                    self.server_offset = 0
                    if self.current_server not in server_list:
                        logger.warning("[refresh-server] %s is not effective, change one" % str(self.current_server))
                        self.current_server = server_list[self.server_offset]
            except Exception as e:
                logger.exception("[refresh-server] exception %s occur" % str(e))

    def change_server(self):
        with self.server_list_lock:
            self.server_offset = (self.server_offset + 1) % len(self.server_list)
            self.current_server = self.server_list[self.server_offset]

    def get_server(self):
        if self.server_list is None:
            with self.server_list_lock:
                logger.info("[get-server] server list is null, try to initialize")
                server_list = get_server_list(self.endpoint, 443 if self.tls_enabled else 8080, self.cai_enabled)
                if not server_list:
                    logger.error("[get-server] empty server_list get from %s" % self.endpoint)
                    return None
                self.server_list = server_list
                self.current_server = self.server_list[self.server_offset]
                logger.info("[get-server] server_num:%s server_list:%s" % (len(self.server_list), self.server_list))

            if self.cai_enabled:
                t = Thread(target=self._refresh_server_list)
                t.setDaemon(True)
                t.start()

        logger.info("[get-server] use server:%s" % str(self.current_server))
        return self.current_server

    def get(self, data_id, group, timeout=None):
        """Get value of one config item.

        query priority:
        1.  get from local failover dir(default: "{cwd}/acm/data")
            failover dir can be manually copied from snapshot dir(default: "{cwd}/acm/snapshot") in advance
            this helps to suppress the effect of known server failure

        2.  get from one server until value is got or all servers tried
            content will be save to snapshot dir

        3.  get from snapshot dir

        :param data_id: dataId
        :param group: group, use "DEFAULT_GROUP" if no group specified
        :param timeout: timeout for requesting server in seconds
        :return: value
        """
        data_id, group = process_common_params(data_id, group)
        logger.info("[get-config] data_id:%s, group:%s, namespace:%s, timeout:%s" % (
            data_id, group, self.namespace, timeout))

        params = {
            "dataId": data_id,
            "group": group,
            "tenant": self.namespace
        }

        cache_key = group_key(data_id, group, self.namespace)
        # get from failover
        content = read_file(self.failover_base, cache_key)
        if content is None:
            logger.debug("[get-config] failover config is not exist for %s, try to get from server" % cache_key)
        else:
            logger.debug("[get-config] get %s from failover directory, content is %s" % (cache_key, truncate(content)))
            return content

        # get from server
        try:
            resp = self._do_sync_req("/diamond-server/config.co", None, params, None, timeout or self.default_timeout)
            content = resp.read().decode("GBK")
            logger.info(
                "[get-config] content from server:%s, data_id:%s, group:%s, namespace:%s, try to save snapshot" % (
                    truncate(content), data_id, group, self.namespace))
            save_file(self.snapshot_base, cache_key, content)
            return content
        except HTTPError as e:
            if e.code == HTTPStatus.NOT_FOUND:
                logger.warning(
                    "[get-config] config not found for data_id:%s, group:%s, namespace:%s, try to delete snapshot" % (
                        data_id, group, self.namespace))
                delete_file(self.snapshot_base, cache_key)
                return None
            elif e.code == HTTPStatus.CONFLICT:
                logger.error(
                    "[get-config] config being modified concurrently for data_id:%s, group:%s, namespace:%s" % (
                        data_id, group, self.namespace))
            elif e.code == HTTPStatus.FORBIDDEN:
                logger.error("[get-config] no right for data_id:%s, group:%s, namespace:%s" % (
                    data_id, group, self.namespace))
                raise ACMException("Insufficient privilege.")
            else:
                logger.error("[get-config] error code [:%s] for data_id:%s, group:%s, namespace:%s" % (
                    e.code, data_id, group, self.namespace))
        except ACMException as e:
            logger.error("[get-config] acm exception: %s" % str(e))
        except Exception as e:
            logger.exception("[get-config] exception %s occur" % str(e))

        logger.error("[get-config] get config from server failed, try snapshot, data_id:%s, group:%s, namespace:%s" % (
            data_id, group, self.namespace))
        content = read_file(self.snapshot_base, cache_key)
        if content is None:
            logger.warning("[get-config] snapshot is not exist for %s." % cache_key)
        else:
            logger.debug("[get-config] get %s from snapshot directory, content is %s" % (cache_key, truncate(content)))
            return content

    @synchronized_with_attr("pulling_lock")
    def add_watcher(self, data_id, group, cb):
        self.add_watchers(data_id, group, [cb])

    @synchronized_with_attr("pulling_lock")
    def add_watchers(self, data_id, group, cb_list):
        """Add watchers to specified item.

        1.  callback is invoked from current process concurrently by thread pool
        2.  callback is invoked at once if the item exists
        3.  callback is invoked if changes or deletion detected on the item

        :param data_id: data_id
        :param group: group, use "DEFAULT_GROUP" if no group specified
        :param cb_list: callback functions
        :return:
        """
        if not cb_list:
            raise ACMException("A callback function is needed.")
        data_id, group = process_common_params(data_id, group)
        logger.info("[add-watcher] data_id:%s, group:%s, namespace:%s" % (data_id, group, self.namespace))
        cache_key = group_key(data_id, group, self.namespace)
        wl = self.watcher_mapping.get(cache_key)
        if not wl:
            wl = list()
            self.watcher_mapping[cache_key] = wl
        for cb in cb_list:
            wl.append(WatcherWrap(cache_key, cb))
            logger.info("[add-watcher] watcher has been added for key:%s, new callback is:%s, callback number is:%s" % (
                cache_key, cb.__name__, len(wl)))

        if self.puller_mapping is None:
            logger.debug("[add-watcher] pulling should be initialized")
            self._int_pulling()

        if cache_key in self.puller_mapping:
            logger.debug("[add-watcher] key:%s is already in pulling" % cache_key)
            return

        for key, puller_info in self.puller_mapping.items():
            if len(puller_info[1]) < self.pulling_config_size:
                logger.debug("[add-watcher] puller:%s is available, add key:%s" % (puller_info[0], cache_key))
                puller_info[1].append(key)
                self.puller_mapping[cache_key] = puller_info
                break
        else:
            logger.debug("[add-watcher] no puller available, new one and add key:%s" % cache_key)
            key_list = self.process_mgr.list()
            key_list.append(cache_key)
            puller = Process(target=self._do_pulling, args=(key_list, self.notify_queue))
            puller.daemon = True
            puller.start()
            self.puller_mapping[cache_key] = (puller, key_list)

    @synchronized_with_attr("pulling_lock")
    def remove_watcher(self, data_id, group, cb, remove_all=False):
        """Remove watcher from specified key

        :param data_id: data_id
        :param group: group, use "DEFAULT_GROUP" if no group specified
        :param cb: callback function
        :param remove_all: weather to remove all occurrence of the callback or just once
        :return:
        """
        if not cb:
            raise ACMException("A callback function is needed.")
        data_id, group = process_common_params(data_id, group)
        if not self.puller_mapping:
            logger.warning("[remove-watcher] watcher is never started.")
            return
        cache_key = group_key(data_id, group, self.namespace)
        wl = self.watcher_mapping.get(cache_key)
        if not wl:
            logger.warning("[remove-watcher] there is no watcher on key:%s" % cache_key)
            return

        wrap_to_remove = list()
        for i in wl:
            if i.callback == cb:
                wrap_to_remove.append(i)
                if not remove_all:
                    break

        for i in wrap_to_remove:
            wl.remove(i)

        logger.info("[remove-watcher] %s is removed from %s, remove all:%s" % (cb.__name__, cache_key, remove_all))
        if not wl:
            logger.debug("[remove-watcher] there is no watcher for:%s, kick out from pulling" % cache_key)
            self.watcher_mapping.pop(cache_key)
            puller_info = self.puller_mapping[cache_key]
            puller_info[1].remove(cache_key)
            if not puller_info[1]:
                logger.debug("[remove-watcher] there is no pulling keys for puller:%s, stop it" % puller_info[0])
                self.puller_mapping.pop(cache_key)
                puller_info[0].terminate()

    def _do_sync_req(self, url, headers=None, params=None, data=None, timeout=None):
        url = "?".join([url, urlencode(params)]) if params else url
        all_headers = self._get_common_headers(params)
        if headers:
            all_headers.update(headers)
        logger.debug(
            "[do-sync-req] url:%s, headers:%s, params:%s, data:%s, timeout:%s" % (
                url, all_headers, params, data, timeout))
        tries = 0
        while True:
            try:
                server_info = self.get_server()
                if not server_info:
                    logger.error("[do-sync-req] can not get one server.")
                    raise ACMException("Server is not available.")
                address, port, is_ip_address = server_info
                server = ":".join([address, str(port)])
                # if tls is enabled and server address is in ip, turn off verification
                if self.tls_enabled and is_ip_address:
                    context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
                    context.check_hostname = False
                else:
                    context = None
                server_url = "%s://%s" % ("https" if self.tls_enabled else "http", server)
                req = Request(url=server_url + url, data=data, headers=all_headers)
                resp = urlopen(req, timeout=timeout, context=context)
                logger.debug("[do-sync-req] info from server:%s" % server)
                return resp
            except HTTPError as e:
                if e.code in {HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.BAD_GATEWAY,
                              HTTPStatus.SERVICE_UNAVAILABLE}:
                    logger.warning("[do-sync-req] server:%s is not available for reason:%s" % (server, e.msg))
                else:
                    raise
            except socket.timeout:
                logger.warning("[do-sync-req] %s request timeout" % server)
            except URLError as e:
                logger.warning("[do-sync-req] %s connection error:%s" % (server, e.reason))

            tries += 1
            if tries >= len(self.server_list):
                logger.error("[do-sync-req] %s maybe down, no server is currently available" % server)
                raise ACMException("All server are not available")
            self.change_server()
            logger.warning("[do-sync-req] %s maybe down, skip to next" % server)

    def _do_pulling(self, cache_list, queue):
        cache_pool = dict()
        for cache_key in cache_list:
            cache_pool[cache_key] = CacheData(cache_key, self)

        while cache_list:
            unused_keys = set(cache_pool.keys())
            contains_init_key = False
            probe_update_string = ""
            for cache_key in cache_list:
                cache_data = cache_pool.get(cache_key)
                if not cache_data:
                    logger.debug("[do-pulling] new key added: %s" % cache_key)
                    cache_data = CacheData(cache_key, self)
                    cache_pool[cache_key] = cache_data
                if cache_data.is_init:
                    contains_init_key = True
                data_id, group, namespace = parse_key(cache_key)
                probe_update_string += WORD_SEPARATOR.join(
                    [data_id, group, cache_data.md5 or "", self.namespace]) + LINE_SEPARATOR
                unused_keys.remove(cache_key)
            for k in unused_keys:
                logger.debug("[do-pulling] %s is no longer watched, remove from cache" % k)
                cache_pool.pop(k)

            logger.debug(
                "[do-pulling] try to detected change from server probe string is %s" % truncate(probe_update_string))
            headers = {"longPullingTimeout": int(self.pulling_timeout * 1000)}
            if contains_init_key:
                headers["longPullingNoHangUp"] = "true"

            data = urlencode({"Probe-Modify-Request": probe_update_string}).encode()

            changed_keys = list()
            try:
                resp = self._do_sync_req("/diamond-server/config.co", headers, None, data, self.pulling_timeout + 10)
                changed_keys = {group_key(*i) for i in parse_pulling_result(resp.read())}
                logger.debug("[do-pulling] following keys are changed from server %s" % truncate(str(changed_keys)))
            except ACMException as e:
                logger.error("[do-pulling] acm exception: %s" % str(e))
            except Exception as e:
                logger.exception("[do-pulling] exception %s occur, return empty list" % str(e))

            for cache_key, cache_data in cache_pool.items():
                cache_data.is_init = False
                if cache_key in changed_keys:
                    data_id, group, namespace = parse_key(cache_key)
                    content = self.get(data_id, group)
                    md5 = hashlib.md5(content.encode()).hexdigest() if content is not None else None
                    cache_data.md5 = md5
                queue.put((cache_key, cache_data.content, cache_data.md5))

    @synchronized_with_attr("pulling_lock")
    def _int_pulling(self):
        if self.puller_mapping is not None:
            logger.info("[init-pulling] puller is already initialized")
            return
        self.puller_mapping = dict()
        self.notify_queue = Queue()
        self.callback_tread_pool = pool.ThreadPool(self.callback_tread_num)
        self.process_mgr = Manager()
        t = Thread(target=self._process_polling_result)
        t.setDaemon(True)
        t.start()
        logger.info("[init-pulling] init completed")

    def _process_polling_result(self):
        while True:
            cache_key, content, md5 = self.notify_queue.get()
            logger.debug("[process-polling-result] receive an event:%s" % cache_key)
            wl = self.watcher_mapping.get(cache_key)
            if not wl:
                logger.warning("[process-polling-result] no watcher on %s, ignored" % cache_key)
                continue

            data_id, group, namespace = parse_key(cache_key)
            params = {
                "data_id": data_id,
                "group": group,
                "namespace": namespace,
                "content": content
            }
            for watcher in wl:
                if not watcher.last_md5 == md5:
                    logger.debug(
                        "[process-polling-result] md5 changed since last call, calling %s" % watcher.callback.__name__)
                    try:
                        self.callback_tread_pool.apply(watcher.callback, (params,))
                    except Exception as e:
                        logger.exception("[process-polling-result] exception %s occur while calling %s " % (
                            str(e), watcher.callback.__name__))
                    watcher.last_md5 = md5

    def _get_common_headers(self, params):
        headers = {
            "Diamond-Client-AppName": self.app_name,
            "Client-Version": VERSION,
            "Content-Type": "application/x-www-form-urlencoded; charset=GBK",
            "exConfigInfo": "true",
        }
        if self.auth_enabled:
            ts = str(int(time.time() * 1000))
            headers.update({
                "Spas-AccessKey": self.ak,
                "timeStamp": ts,
            })
            sign_str = ""
            # in case tenant or group is null
            if not params:
                return headers

            if "tenant" in params:
                sign_str = params["tenant"] + "+"
            if "group" in params:
                sign_str = sign_str + params["group"] + "+"
            if sign_str:
                sign_str += ts
                headers["Spas-Signature"] = base64.encodebytes(
                    hmac.new(self.sk.encode(), sign_str.encode(), digestmod=hashlib.sha1).digest()).decode().strip()
        return headers


if DEBUG:
    ACMClient.set_debugging()
