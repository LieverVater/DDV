#
# Copyright IBM Corp. 2014
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Author: Daniel Bolanos
# Date:   2015

# coding=utf-8
import json                        # json
import threading                   # multi threading
import os                          # for listing directories
import queue as Queue              # queue used for thread syncronization
import sys                         # system calls
import argparse                    # for parsing arguments
import base64                      # necessary to encode in base64
#                                  # according to the RFC2045 standard
import requests                    # python HTTP requests library

# WebSockets
from autobahn.twisted.websocket import WebSocketClientProtocol, \
    WebSocketClientFactory, connectWS
from twisted.python import log
from twisted.internet import ssl, reactor

try:
    raw_input          # Python 2
except NameError:
    raw_input = input  # Python 3


class Utils:

    @staticmethod
    def getAuthenticationToken(hostname, serviceName, username, password):

        fmt = hostname + "{0}/authorization/api/v1/token?url={0}/{1}/api"
        uri = fmt.format(hostname, serviceName)
        uri = uri.replace("wss://", "https://").replace("ws://", "https://")
        print(uri)
        auth = (username, password)
        headers = {'Accept': 'application/json'}
        resp = requests.get(uri, auth=auth, verify=False, headers=headers,
                            timeout=(30, 30))
        print(resp.text)
        jsonObject = resp.json()
        return jsonObject['token']


class WSInterfaceFactory(WebSocketClientFactory):

    def __init__(self, queue, summary, dirOutput, contentType, model,
                 url=None, headers=None, debug=None):

        WebSocketClientFactory.__init__(self, url=url, headers=headers)
        self.queue = queue
        self.summary = summary
        self.dirOutput = dirOutput
        self.contentType = contentType
        self.model = model
        self.queueProto = Queue.Queue()

        self.openHandshakeTimeout = 10
        self.closeHandshakeTimeout = 10

        # start the thread that takes care of ending the reactor so
        # the script can finish automatically (without ctrl+c)
        endingThread = threading.Thread(target=self.endReactor, args=())
        endingThread.daemon = True
        endingThread.start()

    def prepareUtterance(self):

        try:
            utt = self.queue.get_nowait()
            self.queueProto.put(utt)
            return True
        except Queue.Empty:
            print("getUtterance: no more utterances to process, queue is "
                  "empty!")
            return False

    def endReactor(self):

        self.queue.join()
        print("about to stop the reactor!")
        reactor.stop()

    # this function gets called every time connectWS is called (once
    # per WebSocket connection/session)
    def buildProtocol(self, addr):

        try:
            utt = self.queueProto.get_nowait()
            proto = WSInterfaceProtocol(self, self.queue, self.summary,
                                        self.dirOutput, self.contentType)
            proto.setUtterance(utt)
            return proto
        except Queue.Empty:
            print("queue should not be empty, otherwise this function should "
                  "not have been called")
            return None


# WebSockets interface to the STT service
#
# note: an object of this class is created for each WebSocket
# connection, every time we call connectWS
class WSInterfaceProtocol(WebSocketClientProtocol):

    def __init__(self, factory, queue, summary, dirOutput, contentType):
        self.factory = factory
        self.queue = queue
        self.summary = summary
        self.dirOutput = dirOutput
        self.contentType = contentType
        self.packetRate = 20
        self.listeningMessages = 0
        self.timeFirstInterim = -1
        self.bytesSent = 0
        self.chunkSize = 2000     # in bytes
        super(self.__class__, self).__init__()
        print(dirOutput)
        print("contentType: {} queueSize: {}".format(self.contentType,
                                                     self.queue.qsize()))

    def setUtterance(self, utt):

        self.uttNumber = utt[0]
        self.uttFilename = utt[1]
        self.summary[self.uttNumber] = {"hypothesis": "", "timestamps": "", "speaker": "",
                                        "status": {"code": "", "reason": ""}}
        self.fileJson = "{}/{}.json.txt".format(self.dirOutput, self.uttNumber)

    # helper method that sends a chunk of audio if needed (as required
    # what the specified pacing is)
    def maybeSendChunk(self, data):

        def sendChunk(chunk, final=False):
            self.bytesSent += len(chunk)
            self.sendMessage(chunk, isBinary=True)
            if final:
                self.sendMessage(b'', isBinary=True)

        if (self.bytesSent + self.chunkSize >= len(data)):
            if (len(data) > self.bytesSent):
                sendChunk(data[self.bytesSent:len(data)], True)
                return
        sendChunk(data[self.bytesSent:self.bytesSent + self.chunkSize])
        self.factory.reactor.callLater(0.01, self.maybeSendChunk, data=data)
        return

    def onConnect(self, response):
        print("onConnect, server connected: {}".format(response.peer))

    def onOpen(self):
        print("onOpen")
        data = {"action": "start",
                "content-type": str(self.contentType),
                "continuous": True,
                "interim_results": True,
                "inactivity_timeout": 600,
                'max_alternatives': 3,
                'timestamps': True,
                'word_confidence': True,
                'speaker_labels': args.multispeaker == True}
        print("sendMessage(init)")
        # send the initialization parameters
        self.sendMessage(json.dumps(data).encode('utf8'))

        # start sending audio right away (it will get buffered in the
        # STT service)
        print(self.uttFilename)
        with open(str(self.uttFilename), 'rb') as f:
            self.bytesSent = 0
            dataFile = f.read()
        self.maybeSendChunk(dataFile)
        print("onOpen ends")

    def onMessage(self, payload, isBinary):

        if isBinary:
            print("Binary message received: {0} bytes".format(len(payload)))
        else:
            # if uninitialized, receive the initialization response
            # from the server
            jsonObject = json.loads(payload.decode('utf8'))
            if 'state' in jsonObject:
                self.listeningMessages += 1
                if self.listeningMessages == 2:
                    # close the connection
                    self.sendClose(1000)

            # if in streaming
            elif 'results' in jsonObject:
                jsonObject = json.loads(payload.decode('utf8'))
                hypothesis = ""
                # empty hypothesis
                if len(jsonObject['results']) == 0:
                    print("empty hypothesis!")
                # regular hypothesis
                else:
                    # dump the message to the output directory
                    jsonObject = json.loads(payload.decode('utf8'))

                    res = jsonObject['results'][0]
                    hypothesis = res['alternatives'][0]['transcript']
                    timestamps = res['alternatives'][0]['timestamps']
                    bFinal = (res['final'] is True)
                    if bFinal:
                        self.summary[self.uttNumber]['hypothesis'] += hypothesis
                        for list in timestamps:
                            self.summary[self.uttNumber]['timestamps'] += (";".join([str(e) for e in list]) + ",")
                    else:
                        print('interim hyp: "' + hypothesis + '"')
            if 'speaker_labels' in jsonObject:
                labels = jsonObject['speaker_labels']
                for label in labels:
                    if not (str(label['from']) + ";" + str(label['to'])) in self.summary[self.uttNumber]['speaker']:
                        self.summary[self.uttNumber]['speaker'] += str(label['speaker']) + ";" + str(label['from']) + ";" + str(label['to']) + ","
                    if label['final'] == True:
                        self.summary[self.uttNumber]['speaker'] = self.summary[self.uttNumber]['speaker'][0:-1]

    def onClose(self, wasClean, code, reason):

        print("\nonClose")
        print("WebSocket connection closed: {0}, code: {1}, clean: {2}, "
              "reason: {0}".format(reason, code, wasClean))
        self.summary[self.uttNumber]['status']['code'] = code
        self.summary[self.uttNumber]['status']['reason'] = reason

        # create a new WebSocket connection if there are still
        # utterances in the queue that need to be processed
        self.queue.task_done()

        if not self.factory.prepareUtterance():
            return

        # SSL client context: default
        if self.factory.isSecure:
            contextFactory = ssl.ClientContextFactory()
        else:
            contextFactory = None
        connectWS(self.factory, contextFactory)


# function to check that a value is a positive integer
def check_positive_int(value):
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(
            '"%s" is an invalid positive int value' % value)
    return ivalue


# function to check the credentials format
def check_credentials(credentials):
    elements = credentials.split(":")
    if len(elements) == 2:
        return elements
    else:
        raise argparse.ArgumentTypeError(
            '"%s" is not a valid format for the credentials ' % credentials)


if __name__ == '__main__':

    # parse command line parameters
    parser = argparse.ArgumentParser(
        description=('client to do speech recognition using the WebSocket '
                     'interface to the Watson STT service'))
    parser.add_argument(
        '-credentials', action='store', dest='credentials',
        help="Basic Authentication credentials in the form 'username:password'",
        required=True, type=check_credentials)
    parser.add_argument(
        '-in', action='store', dest='fileInput', default='./recordings.txt',
        help='text file containing audio files')
    parser.add_argument(
        '-out', action='store', dest='dirOutput', default='./output',
        help='output directory')
    parser.add_argument(
        '-type', action='store', dest='contentType', default='audio/wav',
        help='audio content type, for example: \'audio/l16; rate=44100\'')
    parser.add_argument(
        '-model', action='store', dest='model', default='en-US_BroadbandModel',
        help='STT model that will be used')
    parser.add_argument(
        '-amcustom', action='store', dest='am_custom_id', default=None,
        help='id of the acoustic model customization that will be used', required=False)
    parser.add_argument(
        '-lmcustom', action='store', dest='lm_custom_id', default=None,
        help='id of the language model customization that will be used', required=False)
    parser.add_argument(
        '-threads', action='store', dest='threads', default='1',
        help='number of simultaneous STT sessions', type=check_positive_int)
    parser.add_argument(
        '-optout', action='store_true', dest='optOut',
        help=('specify opt-out header so user data, such as speech and '
              'hypotheses are not logged into the server'))
    parser.add_argument(
        '-tokenauth', action='store_true', dest='tokenauth',
        help='use token based authentication')
    parser.add_argument(
        '-multispeaker', action='store', dest='multispeaker', default=False,
        help='text file containing audio files')

    args = parser.parse_args()

    # create output directory if necessary
    if os.path.isdir(args.dirOutput):
        fmt = 'the output directory "{}" already exists, overwrite? (y/n)? '
        # while True:
        #     answer = raw_input(fmt.format(args.dirOutput)).strip().lower()
        #     if answer == "n":
        #         sys.stderr.write("exiting...")
        #         sys.exit()
        #     elif answer == "y":
        #         break
    else:
        os.makedirs(args.dirOutput)

    # logging
    log.startLogging(sys.stdout)

    # add audio files to the processing queue
    q = Queue.Queue()
    if args.fileInput[-4:] == ".txt":
        lines = [line.rstrip('\n') for line in open(args.fileInput)]
        fileNumber = 0
        for fileName in lines:
            print(fileName)
            q.put((fileNumber, fileName))
            fileNumber += 1
    else:
        q.put((1, args.fileInput))

    hostname = "stream.watsonplatform.net"
    headers = {'X-WDC-PL-OPT-OUT': '1'} if args.optOut else {}

    # authentication header
    if args.tokenauth:
        headers['X-Watson-Authorization-Token'] = (
            Utils.getAuthenticationToken('https://' + hostname,
                                         'speech-to-text',
                                         args.credentials[0],
                                         args.credentials[1]))
    else:
        auth = args.credentials[0] + ":" + args.credentials[1]
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode('utf-8')

    print(headers)
    # create a WS server factory with our protocol
    fmt = "wss://{}/speech-to-text/api/v1/recognize?model={}"
    url = fmt.format(hostname, args.model)
    if args.am_custom_id != None:
        url += "&acoustic_customization_id=" + args.am_custom_id
    if args.lm_custom_id != None:
        url += "&customization_id=" + args.lm_custom_id
    print(url)
    summary = {}
    factory = WSInterfaceFactory(q, summary, args.dirOutput, args.contentType,
                                 args.model, url, headers, debug=False)
    factory.protocol = WSInterfaceProtocol

    for i in range(min(int(args.threads), q.qsize())):

        factory.prepareUtterance()

        # SSL client context: default
        if factory.isSecure:
            contextFactory = ssl.ClientContextFactory()
        else:
            contextFactory = None
        connectWS(factory, contextFactory)

    reactor.run()

    # dump the hypotheses to the output file
    fileHypotheses = args.dirOutput + "/hypotheses.txt"
    fileStamps = args.dirOutput + "/timestamps.txt"
    f = open(fileHypotheses, "w+")
    f2 = open(fileStamps, "w+")
    successful = 0
    emptyHypotheses = 0
    counter = 0
    print('\n')
    if args.multispeaker == True:
        for key, value in enumerate(sorted(summary.items())):
            value = value[1]
            if value['status']['code'] == 1000:
                print('{}: {}\n'.format(counter + 1, value['hypothesis']))
                successful += 1
                if value['hypothesis'][0] == "":
                    emptyHypotheses += 1
            else:
                fmt = '{}: {status[code]} REASON: {status[reason]}'
                print(fmt.format(key, value['status']['code']))
            speakerList = []
            for values in value['speaker'].split(","):
                speaker = int(values.split(";")[0])
                if speaker not in speakerList:
                    speakerList.append(speaker)
            speakerList.sort()
            hypotheses = ["" for i in range(len(speakerList))]
            stamps = ["" for i in range(len(speakerList))]
            for s, t in zip(value['speaker'].split(","), value['timestamps'].split(",")[:-1]):
                speaker = int(s.split(";")[0])
                timestamps = s.split(";")[1:]
                if t.split(";")[1:] == timestamps:
                    hypotheses[speakerList.index(speaker)] += (t.split(";")[0] + " ")
                    stamps[speakerList.index(speaker)] += t

            for i in range(len(hypotheses)):
                f.write(hypotheses[i].strip() + "\n")
                f2.write(stamps[i].strip()[:-1] + "\n")
            counter += 1
    else:
        for key, value in enumerate(sorted(summary.items())):
            value = value[1]
            if value['status']['code'] == 1000:
                print('{}: {}\n'.format(counter + 1, value['hypothesis']))
                successful += 1
                if value['hypothesis'][0] == "":
                    emptyHypotheses += 1
            else:
                fmt = '{}: {status[code]} REASON: {status[reason]}'
                print(fmt.format(key, value['status']['code']))

            f.write(value['hypothesis'].strip())
            f2.write(value['timestamps'].strip()[:-1])
            counter += 1
    f.close()
    fmt = "successful sessions: {} ({} errors) ({} empty hypotheses)"
    print(fmt.format(successful, len(summary) - successful, emptyHypotheses))