#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright (C) 2014  Lionel Bergeret
#
# ----------------------------------------------------------------
# The contents of this file are distributed under the CC0 license.
# See http://creativecommons.org/publicdomain/zero/1.0/
# ----------------------------------------------------------------
import hid
import time
import os, sys
from datetime import datetime
from pytz import timezone
from optparse import OptionParser
import threading
import ConfigParser

dbSupport = False
try:
    from pymongo import MongoClient
    dbSupport = True
except:
    print "No MongoDB support"
    pass

zulu_fmt = "%Y-%m-%dT%H:%M:%SZ"

# Global definitions
LOGGING_INTERVAL = 60.0
COUNTRATE_INTERVAL = 1.0
PASSCOUNTS_INTERVAL = 0.1
USB_VENDOR_ID = 0x04d8
USB_PRODUCT_ID = 0x100

#
# SPE file export
#
def export2SPE(filename, deviceId, channels, realtime, livetime):
    speFile = open(filename, "w")
    speFile.write("$SPEC_REM:\n")
    speFile.write("#timestamp,device_ID,realtime,livetime,totalcount\n")
    speFile.write("%s,%s,%0.3f,%0.3f,%d\n" % (datetime.now(timezone('UTC')).strftime(zulu_fmt), deviceId, realtime, livetime, sum(channels)))
    speFile.write("$MEAS_TIM:\n")
    speFile.write("%d %d\n" % (int(realtime), int(livetime)))
    speFile.write("$DATA:\n")
    speFile.write("0 4095\n")
    for i in range(4096):
        speFile.write("%d\n" % channels[i])

    # From multispec tool for RadAngel (calibration data)
    speFile.write("""$ENER_FIT:
-357.199955175409 0.969844070381318
$ENER_DATA:
2
494.1 122
1050.47809878844 661.6
$KROMEK_INFO:
LLD:
402
SCO:
off
PRODUCT_FAMILY:
RADANGEL
DETECTOR_TYPE:
RA4S
DETECTOR_TYPE_ID:
256""")
    speFile.close()

#
# HID device enumerate
#
def HIDDeviceList():
    usbPathList = []
    # Enumarate HID devices
    for d in hid.enumerate(0, 0):
        keys = d.keys()
        keys.sort()
        if d["vendor_id"] == USB_VENDOR_ID:
           usbPathList.append(d["path"])
    return usbPathList

#
# Configuration file
#
class RadAngelConfiguration():
    def __init__(self, filename):
      config = ConfigParser.ConfigParser()
      config.read(os.path.realpath(os.path.dirname(sys.argv[0]))+"/"+filename)

      if "radangel" in config.sections():
        self.db_host = config.get('radangel', 'db_host')
        self.db_port = config.getint('radangel', 'db_port')
        self.db_name = config.get('radangel', 'db_name')
        self.db_user = config.get('radangel', 'db_user')
        self.db_passwd = config.get('radangel', 'db_passwd')
        self.loggingInterval = config.getfloat('radangel', 'logging_interval')
      else:
        print "Configuration file is missing"
        sys.exit(0)

      self.devices = {}
      if "device" in config.sections():
        for path, serial in config.items("device"):
            self.devices[path.replace("_",":").lower()] = serial

#
# RadAngel processing class
#
class RadAngel():
    #
    # USB read thread
    #
    class USBReadThread(threading.Thread):
        def __init__(self, hidDevice, radAngelInstance):
            threading.Thread.__init__(self)
            self.hidDevice = hidDevice
            self.radAngelInstance = radAngelInstance
            self.Terminated = False
        def run(self):
            while not self.Terminated:
                d = self.hidDevice.read(62, timeout_ms = 50)
                if d:
                    #print d
                    self.radAngelInstance.ratecounter += 1
                    self.radAngelInstance.totalcounter += 1

                    channel = (d[1]*256+d[2])/16 # ((d[1] << 8 | d[2]) >> 4) = 12bit channel
                    if channel not in self.radAngelInstance.counts:
                        self.radAngelInstance.counts[channel] = 1
                    else:
                        self.radAngelInstance.counts[channel] += 1
                time.sleep(0.0001) # force yield for other threads
        def stop(self):
            self.Terminated = True

    def __init__(self, config, deviceId, devicePath, logFilename, useDatabase, captureTime, captureCount):
        self.config = config
        self.deviceId = deviceId
        self.devicePath = devicePath
        self.logFilename = logFilename
        self.useDatabase = useDatabase
        self.captureTime = captureTime
        self.captureCount = captureCount

    def logPrint(self, message):
       print "[%s] %s" % (self.deviceId, message)

    #
    # Kromek RAW data processing
    #
    def Process(self):
        # Initialize variables
        usbRead = None
        logfile = None
        hidDevice = None

        countrate = 0.0 # CPS
        livetime = 0.0
        realtime = 0.0
        previousRealtime = 0.0
        previousLivetime = 0.0

        # Initialize counters
        self.counts = {}
        self.ratecounter = 0
        self.totalcounter = 0 # keep track of total counts since start
        channelsTotal = [0 for i in range (4096)]

        if self.useDatabase:
            connection = MongoClient(self.config.db_host, self.config.db_port)
            db = connection[self.config.db_name]
            # MongoLab has user authentication
            db.authenticate(self.config.db_user, self.config.db_passwd)

        try:
            self.logPrint("Opening device id %s [%s]" % (self.deviceId, self.devicePath))
            hidDevice = hid.device()
            hidDevice.open_path(self.devicePath)

            self.logPrint("Manufacturer: %s" % hidDevice.get_manufacturer_string())
            self.logPrint("Product: %s" % hidDevice.get_product_string())

            # Open log file
            self.logPrint("Appending data to %s ..." % self.logFilename)
            logfile = open(self.logFilename, "a", 1)

            # Start timers
            start_time = time.time()
            countrate_start_time = start_time # countrate computation
            passcount_start_time = start_time # realtime, livetime computation

            # Start USB reading thread
            self.logPrint("Start USB reading thread")
            usbRead = RadAngel.USBReadThread(hidDevice, self)
            usbRead.start()

            # Main loop (Control-C to exit)
            while True:
                countrate_elapsed_time = time.time() - countrate_start_time
                if (countrate_elapsed_time >= COUNTRATE_INTERVAL):
                    countrate_start_time = time.time()
                    countrate = float(self.ratecounter) / countrate_elapsed_time;
                    self.ratecounter = 0;

                passcount_elapsed_time = time.time() - passcount_start_time
                if (passcount_elapsed_time >= PASSCOUNTS_INTERVAL):
                    passcount_start_time = time.time()

                    currentRealtime = realtime;
                    elapsed_time = time.time() - start_time
                    realtime = realtime + passcount_elapsed_time;
                    elapased = realtime - currentRealtime;
                    livetime = livetime + elapased * (1.0 - countrate * 1E-05);

                elapsed_time = time.time() - start_time
                if (elapsed_time >= self.config.loggingInterval):
                    start_time = time.time()

                    # Copy the counter so USB read thread can continue
                    loggingCounts = dict(self.counts)
                    loggingCounter = sum([loggingCounts[i] for i in loggingCounts])
                    loggingRealtime = realtime - previousRealtime
                    loggingLivetime = livetime - previousLivetime

                    # Clear counter and channels
                    self.counts = {}
                    previousRealtime = realtime
                    previousLivetime = livetime

                    # Prepare for logging
                    now_utc = datetime.now(timezone('UTC'))
                    spectrum = ["%d" % (loggingCounts[i] if i in loggingCounts else 0) for i in range(4096)]
                    cpm = float(loggingCounter)/loggingLivetime*60.0
                    log = "%s,%s,%0.3f,%0.3f,%0.3f,%s,%s" % (now_utc.strftime(zulu_fmt), self.deviceId, loggingRealtime, loggingLivetime, cpm, loggingCounter, ",".join(spectrum))
                    logfile.write("%s\n" % log)
                    logfile.flush()
                    self.logPrint(log)

                    # Keep union
                    channelsTotal = [x + y for x, y in zip(channelsTotal, [(loggingCounts[i] if i in loggingCounts else 0) for i in range(4096)])]

                    # Upload to database if needed
                    if self.useDatabase:
                        data = {"deviceid": self.deviceId, "date": now_utc, "realtime": loggingRealtime, "livetime": loggingLivetime, "channels": [(loggingCounts[i] if i in loggingCounts else 0) for i in range(4096)], "cpm": cpm, "counts": loggingCounter}
                        db.spectrum.insert(data)

                if ((self.captureTime > 0) and (realtime > self.captureTime)) or ((self.captureCount > 0) and (self.totalcounter > self.captureCount)):
                    # Union latest counts from unfinished period
                    channelsTotal = [x + y for x, y in zip(channelsTotal, [(self.counts[i] if i in self.counts else 0) for i in range(4096)])]

                    self.logPrint("Total captured time %0.3f completed" % realtime)
                    self.logPrint("  realtime = %0.3f, livetime = %0.3f, total count = %d, countrate = %0.3f" % (realtime, livetime, self.totalcounter, countrate))
                    break

                time.sleep(0.0001) # force yield for other threads

        except IOError, ex:
            self.logPrint( ex )
            self.logPrint( "You probably don't have the hard coded test hid. Update the hid.device line" )
            self.logPrint( "in this script with one from the enumeration list output above and try again." )
        finally:
            self.logPrint( "Cleanup resources" )
            if usbRead != None: 
                usbRead.stop()
                usbRead.join()
            if hidDevice != None: hidDevice.close()
            if logfile != None: logfile.close()

        self.logPrint( "Done" )

        return channelsTotal, realtime, livetime

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == '__main__':
  # Process command line options
  parser = OptionParser("Usage: radangel.py [options] <logfile>")

  parser.add_option("-c", "--capturecount",
                      type=int, dest="capturecount", default=0,
                      help="specify the total capture counts (default 0 meaning unlimited)")
  parser.add_option("-d", "--database",
                      action="store_true", dest="database", default=False,
                      help="upload to mongodb database")
  parser.add_option("-e", "--enumerate",
                      action="store_true", dest="enumerate", default=False,
                      help="enumerate USB HID devices only")
  parser.add_option("-i", "--deviceid",
                      type=str, dest="deviceid", default="000000-000000",
                      help="specify the device id (default 000000-000000)")
  parser.add_option("-p", "--path",
                      type=str, dest="path", default=None,
                      help="specify USB HID devices path to capture")
  parser.add_option("-t", "--capturetime",
                      type=int, dest="capturetime", default=0,
                      help="specify the capture time in seconds (default 0 meaning unlimited)")

  (options, args) = parser.parse_args()

  usbPathList = HIDDeviceList()
  print "Available RadAngel devices =", usbPathList
  if options.enumerate:
    sys.exit(0)

  if len(usbPathList) == 0:
    print "No RadAngel device is connected"
    sys.exit(0)

  # Load configuration
  config = RadAngelConfiguration(".radangel.conf")

  # Select device path
  if options.path == None:
    devicepath = usbPathList[0].lower()
  else:
    devicepath = options.path.lower()

  # Select device id
  if (devicepath in config.devices):
    deviceid = config.devices[devicepath]
  else:
    deviceid = options.deviceid

  # Create the logfile name
  if len(args) == 1:
    logFilename = args[0]
  else:
    logFilename = "%s_raw.csv" % deviceid
  speFilename = os.path.splitext(logFilename)[0]+".spe"

  radAngel = RadAngel(config, deviceid, devicepath, logFilename, options.database & dbSupport, options.capturetime, options.capturecount)
  channelsTotal, realtime, livetime = radAngel.Process()
  export2SPE(speFilename, deviceid, channelsTotal, realtime, livetime)
