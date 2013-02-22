circadianLighting
=================

A simple circadian lighting implementation using a Raspberry Pi, some Adafruit RGB leds and the Raphael.js colour picker demos.

usage: 

sudo app.py [-h] [--type DRIVER_TYPE] [--length LED_COUNT]

optional arguments:
  -h, --help          show this help message and exit
  --type DRIVER_TYPE  The model number of the LED driver, eg. ws2801 or lpd6803. default=ws2801
  --length LED_COUNT  The number of LEDs in the chain. default=25


Further information on Raphael.js can be found here ... http://raphaeljs.com/

To install the init script, copy the script to /etc/init.d/circadian and register it using the command 'sudo update-rc.d circadian defaults'.  Now update your app.py path, and ammend any arguments you would like to be used at startup.
