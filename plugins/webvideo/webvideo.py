import Queue
import logging
import os
import shutil
import threading
import time
import urllib
import urllib2
import warnings
import xml.etree.ElementTree as ElementTree

import xmpp
import mind
import config
from plugins.video.video import BaseVideo, VideoDetails
from plugins.video.transcode import tivo_compatible

CLASS_NAME = 'WebVideo'

class WebVideo(BaseVideo):

    CONTENT_TYPE = 'x-not-for/tivo'

    def init(self):
        self.__logger = logging.getLogger('pyTivo.webvideo')
        self.work_queue = Queue.Queue()
        self.download_thread_num = 1
        self.in_progress = {}
        self.in_progress_lock = threading.Lock()

        self.startXMPP()
        self.startWorkerThreads()

    def startXMPP(self):
        m = mind.getMind()
        xmpp_info = m.getXMPPLoginInfo()

        jid=xmpp.protocol.JID(xmpp_info['username'] + '/pyTivo')
        cl=xmpp.Client(
            server=xmpp_info['server'],
            port=xmpp_info['port'],
            debug=[],
        )
        self.__logger.debug(f"Connecting to {xmpp_info['server']}:{xmpp_info['port']}")
        cl.connect()
        cl.RegisterHandler('message', self.processMessage)
        self.__logger.debug(f"Loging in as {xmpp_info['username']}/pyTivo")
        cl.auth(user=jid.getNode(), password=config.get_server('tivo_password'),
                resource='pyTivo')

        cl.sendInitPresence(requestRoster=0)

        for user_name in xmpp_info['presence_list']:
            self.__logger.debug(f'Sending presence to {user_name}')
            jid = xmpp.protocol.JID(user_name)
            cl.sendPresence(jid)

        t = threading.Thread(target=self.processXMPP, args=(cl,))
        t.setDaemon(True)
        t.start()

    def startWorkerThreads(self):
        for _ in range(self.download_thread_num):
            t = threading.Thread(target=self.processDlRequest,
                                 name='webvideo downloader')
            t.setDaemon(True)
            t.start()

        t = threading.Thread(target=self.watchQueue,
                             name='webvideo queue watcher')
        t.setDaemon(True)
        t.start()

    def processXMPP(self, client):
        while client.Process(3):
            pass

    def processMessage(self, sess, mess):
        self.__logger.debug('Got message\n %s' % mess.getBody())
        xmpp_action = ElementTree.fromstring(mess.getBody())

        method_name = 'xmpp_' + xmpp_action.findtext('action').lower()
        if not hasattr(self, method_name):
            return False

        method = getattr(self, method_name)
        method(xmpp_action)

    def watchQueue(self):
        while True:
            self.xmpp_cdsupdate()
            time.sleep(60*15)

    def xmpp_cdsupdate(self, xml=None):
        m = mind.getMind()

        self.in_progress_lock.acquire()
        try:
            for request in m.getDownloadRequests():
                if request['bodyOfferId'] not in self.in_progress:
                    self.__logger.debug(f'Adding request to queue, {request}')
                    self.in_progress[request['bodyOfferId']] = True
                    self.work_queue.put(request)
        finally:
            self.in_progress_lock.release()

    def processDlRequest(self):

        while True:
            data = self.work_queue.get()

            for share_name, settings in config.getShares():
                if settings['type'] == 'webvideo':
                    break
            self.__logger.debug(f'Processing request: {data}')

            path = settings['path']

            file_name = os.path.join(
                path,
                f"{data['bodyOfferId'].replace(':', '-')}-{data['url'].split('/')[-1]}",
            )

            status = self.downloadFile(data['url'], file_name)
            mime = 'video/mpeg'

            if status:
                tsn = data['bodyId'][4:]
                file_info = VideoDetails()

                if config.isHDtivo(tsn):
                    for m in ['video/mp4', 'video/bif']:
                        if tivo_compatible(file_name, tsn, m)[0]:
                            mime = m
                            break

                file_info.update(self.metadata_full(file_name, tsn, mime))

                ip = config.get_ip()
                port = config.getPort()

                data[
                    'url'
                ] = f"http://{ip}:{port}{urllib.quote(f'/{share_name}/{os.path.basename(file_name)}')}"
                data['duration'] = file_info['duration'] / 1000
                data['size'] = file_info['size']

            self.__logger.debug(f'Complete request: {data}')

            m = mind.getMind()
            m.completeDownloadRequest(data, status, mime)

            self.in_progress_lock.acquire()
            try:
                del self.in_progress[data['bodyOfferId']]
            finally:
                self.in_progress_lock.release()

    def downloadFile(self, url, file_path):
        self.__logger.info('Downloading %s to %s' % (url, file_path))

        outfile = open(file_path, 'ab')
        size = os.path.getsize(file_path)
        r = urllib2.Request(url)
        if size:
            r.add_header('Range', 'bytes=%s-' % size)

        try:
            infile = urllib2.urlopen(r)
        except urllib2.HTTPError, e:
            if not e.code == 416:
                self.__logger.error('Downloading %s: %d' % (url, e.code))
                outfile.close()
                return False
            infile = urllib2.urlopen(url)
            if int(infile.info()['Content-Length']) == size:
                self.__logger.debug('File was already done. %s' % url)
                return True
            else:
                self.__logger.debug('File was not done but could not resume. %s'
                                    % url)
                outfile.close()
                outfile = open(file_path, 'wb')

        shutil.copyfileobj(infile, outfile, 8192)

        self.__logger.info('Done downloading %s to %s' % (url, file_path))
        return True

    def send_file(self, handler, path, query):
        Video.send_file(self, handler, path, query)
        if os.path.exists(path):
            self.__logger.info(f'Deleting file {path}')
            os.remove(path)
