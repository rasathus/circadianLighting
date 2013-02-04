# -*- coding: utf-8 -*-

from __future__ import with_statement
from socket import gethostname
import logging
from logging.handlers import RotatingFileHandler, SMTPHandler
import time
import datetime
import sched
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

time_format = "%H:%M:%S"
# Event times should be in the form of %H:%M:%S
# Event states should be in the form of [Red,Green,Blue]
# Event names should be unique, as they are used for last run information
auto_state_events = [{'event_name' :  'Night Phase', 'event_start_time' : '00:00:00', 'event_end_time' : '07:00:00' , 'event_state' : [0,0,0], 'transition_duration': 500},
                    {'event_name' :  'Sunrise Phase', 'event_start_time' : '07:30:00', 'event_end_time' : '08:00:00' , 'event_state' : [255,188,0], 'transition_duration': 5000},
                    {'event_name' :  'Alert Phase', 'event_start_time' : '08:00:00', 'event_end_time' : '21:00:00' , 'event_state' : [0,78,103], 'transition_duration': 500},
                    {'event_name' :  'Relaxation Phase', 'event_start_time' : '22:00:00', 'event_end_time' : '23:59:00' , 'event_state' : [255,0,0], 'transition_duration': 500},]

# Stolen from http://stackoverflow.com/questions/4296249/how-do-i-convert-a-hex-triplet-to-an-rgb-tuple-and-back
HEX = '0123456789abcdef'
HEX2 = dict((a+b, HEX.index(a)*16 + HEX.index(b)) for a in HEX for b in HEX)

def rgb(triplet):
    triplet = triplet.lower()
    return { 'R' : HEX2[triplet[0:2]], 'G' : HEX2[triplet[2:4]], 'B' : HEX2[triplet[4:6]]}

def triplet(rgb):
    return format((rgb[0]<<16)|(rgb[1]<<8)|rgb[2], '06x')


def unix_time(dt):
    epoch = datetime.utcfromtimestamp(0)
    delta = dt - epoch
    return delta.total_seconds()

def epoch_now():
    epoch = datetime.utcfromtimestamp(0)
    delta = datetime.now() - epoch
    return delta.total_seconds()

class Auto_Event:
    def __init__(self, state, start_time):
        self.start_time = datetime.datetime.time(datetime.datetime.strptime(start_time, time_format))
        self.state = state

class Chain_Communicator:
    
    def __init__(self):
        self.led_chain = ws2801.WS2801_Chain()
        self.manual_queue = Queue.Queue()
        self.auto_queue = Queue.Queue()
        self.next_auto_event = None
        self.state = 'autonomous'
        self.last_ran = {}
        self.led_state = [0,0,0]
        # populate our initial autoqueue state.
        self.populate_auto_queue()

        self.auto_resume = None
        # use a running flag for our while loop
        self.run = True
        
        app.logger.debug("Chain_Communicator starting main_loop.")
        self.loop_instance = Thread(target=self.main_loop)
        self.loop_instance.start()
        app.logger.debug("Chain_Communicator init complete.")

    def populate_auto_queue(self):            
        # clear down the auto queue, don't know how long weve been in manual mode, queue may be stale.
        with self.auto_queue.mutex:
            self.auto_queue.queue.clear()
            self.next_auto_event = None
        # Calculating next state change.
        current_time = datetime.datetime.time(datetime.datetime.now())
        current_date = datetime.datetime.date(datetime.datetime.now())
        app.logger.debug("Running populate_auto_queue @ current time : %s" % current_time)
        for event in auto_state_events:
            if event['event_name'] in self.last_ran.keys():
                if self.last_ran[event['event_name']] != current_date:
                    del self.last_ran[event['event_name']]
                else:
                    time.sleep(0.25)
            
            if event['event_name'] not in self.last_ran.keys():        
                # check to see if we're in the middle of any scheduled events
                start_time = datetime.datetime.time(datetime.datetime.strptime(event['event_start_time'],time_format))
                end_time = datetime.datetime.time(datetime.datetime.strptime(event['event_end_time'],time_format))
                # app.logger.debug("Checking to see whether current time : %s is between event start : %s and event end : %s" % (current_time, start_time, end_time))
                if current_time > start_time and current_time < end_time:
                    # got a match, this is our current event.
                    app.logger.debug("Found a matching event, %s starts at %s and finishes %s" % (event['event_name'], event['event_start_time'], event['event_end_time']))
                    # check we aren't already in that led state.
                    if self.led_state != event['event_state']:
                        self.auto_transition(event['event_state'], transition_duration=event['transition_duration'], start_time=event['event_start_time'])    
                    self.last_ran[event['event_name']] = current_date
                    break
        else:
            app.logger.debug("We don't appear to be within any programmed events.")  
            time.sleep(0.25)

    def main_loop(self):
        app.logger.debug("main_loop - processing queue ...")
        while self.run :
            # What about using two while loops one for while self.state == 'autonomous', 
            # allowing much longer sleeps, and one for manual. will seb call to other thread pull out quick enough.
            if self.state != 'autonomous':
                # assume I need to do something for thread safety here ...
                if not self.manual_queue.empty():
                    # were only ever interested in the most recent manual update, the rest can be discarded.
                    manual_setting = self.manual_queue.get()
                    self.led_chain.set_rgb(manual_setting)
                    self.led_chain.write()
                    self.led_state = manual_setting
                    # !!! change this to minutes when done debugging !!!
                    self.auto_resume = datetime.datetime.now() + datetime.timedelta(seconds=auto_resume_offset)
                    self.manual_queue.task_done()
                else:
                    if datetime.datetime.now() > self.auto_resume:
                        # Resuming autonomous operation, recalculating transitions to autonomous states.
                        self.state = 'autonomous'
                        app.logger.info("Resuming autonomous opperation")
                        self.populate_auto_queue()
                        # clear down our last run queue, as we may need to return to a previosuly executed state.
                        self.last_ran = {}
                    else:
                        time.sleep(0.25)
            else:
                # run through our autonomous actions.
                if self.next_auto_event is None:                
                    if not self.auto_queue.empty():
                        # pop the first off the top of the stack and check its start time
                        self.next_auto_event = self.auto_queue.get()
                    else:
                        self.populate_auto_queue()

                if self.next_auto_event is not None:                
                    current_time = datetime.datetime.time(datetime.datetime.now())
                    if current_time >= self.next_auto_event.start_time:
                        # write out our led state.
                        self.led_chain.set_rgb(self.next_auto_event.state)
                        self.led_chain.write()
                        self.led_state = self.next_auto_event.state
                        app.logger.debug("Auto-Event written out state : %s as event time : %s has passed" % (self.next_auto_event.state, self.next_auto_event.start_time)) 
                        # reset our next event so we grab the next from the queue.
                        self.next_auto_event = None
                    else:
                        app.logger.debug("Auto-Event time : %s is not past current time : %s" % (self.next_auto_event.start_time, current_time)) 
            time.sleep(0.001)    


    def auto_transition(self, state, transition_duration=150, start_time=datetime.datetime.now().strftime(time_format)):
        # States must be in the format of a list containing Red, Green and Blue element values in order.
        # example. White = [255,255,255] Red = [255,0,0] Blue = [0,0,255] etc.
        # a duration is represented in an incredibly imprescise unit, as ticks.
        with self.manual_queue.mutex:
            self.manual_queue.queue.clear()
        app.logger.debug("Current state is : %s , destination state is : %s , transitioning in : %d ticks" % (self.led_state, state, transition_duration))

        # Using a modified version of http://stackoverflow.com/questions/6455372/smooth-transition-between-two-states-dynamically-generated for smooth transitions between states.
        for transition_count in range(transition_duration - 1):
            event_state = []
            for component in range(3):
                event_state.append(self.led_state[component] + (state[component] - self.led_state[component]) * transition_count / transition_duration)
            self.auto_queue.put(Auto_Event(event_state, start_time))

        # last event is always fixed to the destination state to ensure we get there, regardless of any rounding errors. May need to rethink this mechanism, as I suspect small transitions will be prone to rounding errors resulting in a large final jump.
        self.auto_queue.put(Auto_Event(state, start_time))

    def manual_transition(self, state, transition_duration=150):
        # States must be in the format of a list containing Red, Green and Blue element values in order.
        # example. White = [255,255,255] Red = [255,0,0] Blue = [0,0,255] etc.
        # a duration is represented in an incredibly imprescise unit, as ticks.
        with self.manual_queue.mutex:
            self.manual_queue.queue.clear()
        app.logger.debug("Current state is : %s , destination state is : %s , transitioning in : %d ticks" % (self.led_state, state, transition_duration))

        # Using a modified version of http://stackoverflow.com/questions/6455372/smooth-transition-between-two-states-dynamically-generated for smooth transitions between states.
        for transition_count in range(transition_duration - 1):
            event_state = []
            for component in range(3):
                event_state.append(self.led_state[component] + (state[component] - self.led_state[component]) * transition_count / transition_duration)
            self.manual_queue.put(event_state)

        # last event is always fixed to the destination state to ensure we get there, regardless of any rounding errors. May need to rethink this mechanism, as I suspect small transitions will be prone to rounding errors resulting in a large final jump.
        self.manual_queue.put(state)

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
    led_chain.manual_transition([rgb_val['R'],rgb_val['G'],rgb_val['B']])
    led_chain.state = 'manual'
    app.logger.info("Set chain to manual colour state : %s" % [rgb_val['R'],rgb_val['G'],rgb_val['B']])
    return_object['success'] = True
    return jsonify(return_object)   

# add some filters to jinja
app.jinja_env.filters['datetimeformat'] = format_datetime


if __name__ == '__main__':
    # create console handler and set level to debug, with auto log rotate max size 10mb keeping 10 logs.
    file_handler = RotatingFileHandler( LOGGING_PATH , maxBytes=10240000, backupCount=10)

    # create formatter
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    # add formatter to our console handler
    file_handler.setFormatter(log_formatter)

    file_handler.setLevel(logging.DEBUG)
    app.logger.addHandler(file_handler)
    
    led_chain = Chain_Communicator()

    try:
        app.run(host='0.0.0.0', port=8080, use_reloader=False)
    except KeyboardInterrupt:
        app.logger.warning("Caught keyboard interupt.  Shutting down ...")
        led_chain.shutdown()
    except :
        app.logger.exception("Generic error")
