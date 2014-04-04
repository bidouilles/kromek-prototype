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

# Global variables
counts = {}
counts["cpm"] = []
counter = 0
ratecounter = 0
totalcounter = 0 # keep track of total counts since start

#
# USB read thread
#
class USBReadThread(threading.Thread):
    def __init__(self, hidDevice):
        threading.Thread.__init__(self)
        self.hidDevice = hidDevice
        self.Terminated = False
    def run(self):
        global counter, ratecounter, totalcounter, counts
        while not self.Terminated:
            d = self.hidDevice.read(62, timeout_ms = 50)
            if d:
                #print d
                counter += 1
                ratecounter += 1
                totalcounter += 1

                channel = (d[1]*256+d[2])/16 # ((d[1] << 8 | d[2]) >> 4) = 12bit channel
                if channel not in counts:
                    counts[channel] = 1
                else:
                    counts[channel] += 1
    def stop(self):
        self.Terminated = True

#
# SPE file export
#
def export2SPE(filename, deviceId, channels, realtime, livetime):
    speFile = open(filename, "w")
    speFile.write("$SPEC_REM:\n")
    speFile.write("#timestamp,device_ID,realtime,livetime\n")
    speFile.write("%s,%s,%0.3f,%0.3f\n" % (datetime.now(timezone('UTC')).strftime(zulu_fmt), deviceId, realtime, livetime))
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
        # for key in keys:
        #     print "%s : %s" % (key, d[key])
        # print ""
    return usbPathList

#
# Kromek RAW data processing
#
def kromekProcess(deviceId, vendorId, productId, logFilename, useDatabase, captureTime, captureCount, usbHIDPath = None):
    global counter, ratecounter, totalcounter, counts

    # Initialize variables
    usbRead = None
    logfile = None
    hidDevice = None

    countrate = 0.0 # CPS
    livetime = 0.0
    realtime = 0.0

    counts = {}
    counts["cpm"] = []
    for i in range(4096):
        counts[i] = 0
    channelsTotal = [0 for i in range (4096)]

    counter = 0
    ratecounter = 0

    if useDatabase:
        connection = MongoClient("ds030827.mongolab.com", 30827)
        db = connection["kromek"]
        # MongoLab has user authentication
        db.authenticate("kromek", "kromek")

    try:
        print "Opening device"
        hidDevice = hid.device()
        if usbHIDPath == None:
            hidDevice.open(vendorId, productId)
        else:
            hidDevice.open_path(usbHIDPath)

        print "Manufacturer: %s" % hidDevice.get_manufacturer_string()
        print "Product: %s" % hidDevice.get_product_string()
        # print "Serial No: %s" % hidDevice.get_serial_number_string()

        # Open log file
        logfile = open(logFilename, "w")

        # Start timers
        start_time = time.time()
        countrate_start_time = start_time # countrate computation
        passcount_start_time = start_time # realtime, livetime computation

        # Start USB reading thread
        print "Start USB reading thread"
        usbRead = USBReadThread(hidDevice)
        usbRead.start()

        # Main loop (Control-C to exit)
        while True:
            countrate_elapsed_time = time.time() - countrate_start_time
            if (countrate_elapsed_time >= COUNTRATE_INTERVAL):
                countrate_start_time = time.time()
                countrate = float(ratecounter) / countrate_elapsed_time;
                ratecounter = 0;

            passcount_elapsed_time = time.time() - passcount_start_time
            if (passcount_elapsed_time >= PASSCOUNTS_INTERVAL):
                passcount_start_time = time.time()

                currentRealtime = realtime;
                elapsed_time = time.time() - start_time
                realtime = realtime + passcount_elapsed_time;
                elapased = realtime - currentRealtime;
                livetime = livetime + elapased * (1.0 - countrate * 1E-05);

            elapsed_time = time.time() - start_time
            if (elapsed_time >= LOGGING_INTERVAL):
                start_time = time.time()

                # Copy the counter so USB read thread can continue
                loggingCounts = dict(counts)
                loggingCounter = counter

                # Clear counter and channels
                counter = 0
                for i in range(4096): counts[i] = 0

                # Prepare for logging
                now_utc = datetime.now(timezone('UTC'))
                spectrum = ["%d" % (loggingCounts[i] if i in counts else 0) for i in range(4096)]
                log = "%s,%s,%s" % (now_utc.strftime(zulu_fmt), loggingCounter, ",".join(spectrum))
                logfile.write("%s\n" % log)
                counts["cpm"].append(loggingCounter)
                print realtime, livetime
                print log

                # Keep union
                channelsTotal = [x + y for x, y in zip(channelsTotal, [(loggingCounts[i] if i in counts else 0) for i in range(4096)])]

                # Upload to database if needed
                if useDatabase:
                    data = {"deviceid": deviceId, "date": now_utc, "channels": [(loggingCounts[i] if i in counts else 0) for i in range(4096)], "cpm": loggingCounter}
                    db.spectrum.insert(data)

            if ((captureTime > 0) and (realtime > captureTime)) or ((captureCount > 0) and (totalcounter > captureCount)):
                # Union latest counts from unfinished period
                channelsTotal = [x + y for x, y in zip(channelsTotal, [(counts[i] if i in counts else 0) for i in range(4096)])]

                print "Total captured time %0.3f completed" % realtime
                print "  realtime = %0.3f, livetime = %0.3f, total count = %d, countrate = %0.3f" % (realtime, livetime, totalcounter, countrate)
                break

    except IOError, ex:
        print ex
        print "You probably don't have the hard coded test hid. Update the hid.device line"
        print "in this script with one from the enumeration list output above and try again."
    finally:
        print "Cleanup resources"
        if usbRead != None: usbRead.stop()
        if hidDevice != None: hidDevice.close()
        if logfile != None: logfile.close()

    print "Done"

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

  if len(args) == 1:
    logFilename = args[0]
  else:
    logFilename = "radangel.log"
  speFilename = os.path.splitext(logFilename)[0]+".spe"

  usbPathList = HIDDeviceList()
  print "Available RadAngel devices =", usbPathList
  if options.enumerate:
    sys.exit(0)

  channelsTotal, realtime, livetime = kromekProcess(options.deviceid, USB_VENDOR_ID, USB_PRODUCT_ID, logFilename, options.database & dbSupport, options.capturetime, options.capturecount, options.path)
  export2SPE(speFilename, options.deviceid, channelsTotal, realtime, livetime)
