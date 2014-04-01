import hid
import time
from datetime import datetime
from pytz import timezone

zulu_fmt = "%Y-%m-%dT%H:%M:%SZ"

for d in hid.enumerate(0x04d8, 0x100):
    keys = d.keys()
    keys.sort()
    for key in keys:
        print "%s : %s" % (key, d[key])
    print ""

try:
    print "Opening device"
    h = hid.device(0x04d8, 0x100)

    print "Manufacturer: %s" % h.get_manufacturer_string()
    print "Product: %s" % h.get_product_string()
    print "Serial No: %s" % h.get_serial_number_string()

    # try non-blocking mode by uncommenting the next line
    h.set_nonblocking(1)

    start_time = time.time()
    counts = {}
    counts["cpm"] = []
    counter = 0
    while True:
        d = h.read(62)
        if d:
            counter += 1
            channel = (d[1]*256+d[2])/16
            if channel not in counts:
                counts[channel] = 1
            else:
                counts[channel] += 1

        elapsed_time = time.time() - start_time
        if (elapsed_time > 60.0):
            now_utc = datetime.now(timezone('UTC'))
            spectrum = ["%d" % (counts[i] if i in counts else 0) for i in range(4096)]
            print "%s,%s,%s" % (now_utc.strftime(zulu_fmt), counter, ",".join(spectrum))
            counts["cpm"].append(counter)
            counter = 0
            start_time = time.time()

    print "Closing device"
    h.close()

except IOError, ex:
    print ex
    print "You probably don't have the hard coded test hid. Update the hid.device line"
    print "in this script with one from the enumeration list output above and try again."

print "Done"




