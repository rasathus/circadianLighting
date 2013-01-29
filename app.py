# -*- coding: utf-8 -*-

from __future__ import with_statement
from socket import gethostname
import logging
from logging.handlers import RotatingFileHandler, SMTPHandler
import time
from datetime import timedelta, datetime
import json
import Queue
from threading import Thread

from flask import Flask, request, session, url_for, redirect, render_template, abort, g, flash, jsonify

from pigredients.ics import ws2801 as ws2801

SECRET_KEY = 'nkjfsnkgbkfnge347r28fherg8fskgsd2r3fjkenwkg33f3s'
LOGGING_PATH = "moodLight.log"

led_chain = None
auto_resume_offset = 30 # the number of idle minutes before resuming auto operation after a manual override.

# create our little application
app = Flask(__name__)
app.config.from_object(__name__)
app.debug = True

# Stolen from http://stackoverflow.com/questions/4296249/how-do-i-convert-a-hex-triplet-to-an-rgb-tuple-and-back
HEX = '0123456789abcdef'
HEX2 = dict((a+b, HEX.index(a)*16 + HEX.index(b)) for a in HEX for b in HEX)

def rgb(triplet):
    triplet = triplet.lower()
    return { 'R' : HEX2[triplet[0:2]], 'G' : HEX2[triplet[2:4]], 'B' : HEX2[triplet[4:6]]}

def triplet(rgb):
    return format((rgb[0]<<16)|(rgb[1]<<8)|rgb[2], '06x')

class Chain_Communicator:
    
    def __init__(self):
        self.led_chain = ws2801.WS2801_Chain()
        self.manual_queue = []
        self.auto_queue = Queue.Queue()
        self.state = 'autonomous'
        self.auto_resume = None
        # use a running flag for our while loop
        self.run = True
        app.logger.debug("Chain_Communicator starting setter.")
        self.setter_instance = Thread(target=self.setter)
        self.setter_instance.start()
        app.logger.debug("Chain_Communicator init complete.")
    
    def setter(self):
        app.logger.debug("setter - processing queue ...")
        while self.run :
            if self.state != 'autonomous':
                # assume I need to do something for thread safety here ...
                if self.manual_queue:
                    # were only ever interested in the most recent manual update, the rest can be discarded.
                    manual_setting = self.manual_queue.pop()
                    del self.manual_queue[:]
                    self.led_chain.set_rgb(manual_setting)
                    app.logger.info("Set chain to manual colour state : %s" % manual_setting)
                    
                    self.led_chain.write()
                    # !!! change this to minutes when done debugging !!!
                    self.auto_resume = datetime.now() + timedelta(seconds=auto_resume_offset)
                else:
                    if datetime.now() > self.auto_resume:
                        self.state = 'autonomous'
                        app.logger.info("Resuming autonomous opperation")
            else:
                pass

            time.sleep(0.05)    
                    
    def send_message(self, message):
        # Messages must be in the format of a dict, either containing a single 'all' key, or n integer keys representing the ids of leds required to change.  After each 'message' a write command is called to set make visible the changes.
        # example. { 'all' : [255,255,255]} or { 1 : [255,0,255], 5 : [0,255,0], 10 : [0,0,255]} 
        self.manual_queue.append(message)
        self.state = 'manual'

    def shutdown(self):
        app.logger.debug("shutdown - shutdown started ...")
        self.run = False
        app.logger.debug("shutdown - returned from close.")



def format_datetime(timestamp):
    """Format a timestamp for display."""
    return datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d @ %H:%M')

@app.before_request
def before_request():
    pass
        
@app.teardown_request
def teardown_request(exception):
    pass

@app.route('/')
def index():
    return render_template('index.html')
    
@app.route('/set/<hex_val>', methods=['GET', 'POST'])
def send_command(hex_val):
    return_object = {'output' : None , 'error' : None, 'success' : False}
    rgb_val = rgb(hex_val)
    app.logger.debug("Given colour_val : %s, converted it to %s" % (hex_val, rgb_val))
    led_chain.send_message([rgb_val['R'],rgb_val['G'],rgb_val['B']])
    return_object['success'] = True
    return jsonify(return_object)   

"""    
@app.route('/set/<hex_val>', methods=['GET', 'POST'])
def send_command(hex_val):
    return_object = {'output' : None , 'error' : None, 'success' : False}
    rgb_val = rgb(hex_val)
    print "Given colour_val : %s, converted it to %s" % (hex_val, rgb_val)
    led_chain.set_rgb(rgb_value=[rgb_val['R'],rgb_val['G'],rgb_val['B']], lumi=100)
    led_chain.write()
    return_object['success'] = True
    return jsonify(return_object)   
"""

# add some filters to jinja
app.jinja_env.filters['datetimeformat'] = format_datetime


if __name__ == '__main__':
    ADMINS = ['chris@fane.cc']
    if not app.debug:
        mail_handler = SMTPHandler('172.16.1.2',
                                   'server@fane.cc',
                                   ADMINS, 'The Raspberry site experienced a failure')
        mail_handler.setLevel(logging.ERROR)
        app.logger.addHandler(mail_handler)

    # create console handler and set level to debug, with auto log rotate max size 10mb keeping 10 logs.
    file_handler = RotatingFileHandler( LOGGING_PATH , maxBytes=10240000, backupCount=10)

    # create formatter
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    # add formatter to our console handler
    file_handler.setFormatter(log_formatter)

    file_handler.setLevel(logging.DEBUG)
    app.logger.addHandler(file_handler)

    # !! REMOVE ME !!!
    #led_chain = ws2801.WS2801_Chain()
    
    led_chain = Chain_Communicator()

    try:
        app.run(host='0.0.0.0', port=8080, use_reloader=False)
    except KeyboardInterrupt:
        app.logger.warning("Caught keyboard interupt.  Shutting down ...")
        led_chain.shutdown()