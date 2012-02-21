from devicemanagerSUT import DeviceManagerSUT
from optparse import OptionParser
import sys
import os

def main(ip, port, filename):
  dm = DeviceManagerSUT(ip, port)
  dm.pushFile(filename, '/data/data/com.mozilla.SUTAgentAndroid/files/SUTAgent.ini')

parser = OptionParser()
defaults = {}
parser.add_option("-i", "--ip", dest="ip", help="IP address of device")
parser.add_option("-p", "--port", dest="port", help="Agent port on device, defaults to 20701")
defaults["port"] = "20701"
parser.add_option("-f", "--file", dest="file", help="SUTAgent.ini file to copy to device, must be named 'SUTAgent.ini' and defaults to checking in the local directory for the file.")
defaults["file"] = "SUTAgent.ini"
parser.set_defaults(**defaults)

(options, args) = parser.parse_args()

# Verify Options
if (not options.ip or not options.file):
  print "You must specify an ip address for the device and the SUTAgent.ini file"
  sys.exit()

if (not os.path.isfile(options.file)):
  print "The SUTAgent.ini file you are referencing either does not exist or I cannot find it"
  sys.exit()

(head, tail) = os.path.split(options.file)

if (tail != 'SUTAgent.ini'):
  print "The SUTAgent.ini file must be named 'SUTAgent.ini' (case sensitive)"
  sys.exit()

if __name__ == "__main__":
  main(options.ip, options.port, options.file)
