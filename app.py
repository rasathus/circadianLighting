# -*- coding: utf-8 -*-

from __future__ import with_statement
import logging
import time
import json
import Queue

from logging.handlers import RotatingFileHandler
from random import randint
from threading import Thread
from datetime import datetime, timedelta
from flask import Flask, request, session, url_for, redirect, render_template, abort, g, flash, jsonify
from apscheduler.scheduler import Scheduler

from pigredients.ics import ws2801 as ws2801

SECRET_KEY = 'nkjfsnkgbkfnge347r28fherg8fskgsd2r3fjkenwkg33f3s'
LOGGING_PATH = "moodLight.log"

led_chain = None
auto_resume_offset = 30 # the number of idle minutes before resuming auto operation after a manual override.
auto_resume_job = None

# create our little application
app = Flask(__name__)
app.config.from_object(__name__)
app.debug = True # !!! Set this to False for production use !!!

persistent_store_path = '/home/pi/.lighting.db'

config = {'apscheduler.jobstores.file.class': 'apscheduler.jobstores.shelve_store:ShelveJobStore',
          'apscheduler.jobstores.file.path': persistent_store_path}

time_format = "%H:%M:%S"
# Event times should be in the form of %H:%M:%S
# Event states should be in the form of [Red,Green,Blue]
# Event names should be unique, as they are used for last run information
auto_state_events = [{'event_name' :  'Night Phase', 'event_start_time' : '00:00:00', 'event_end_time' : '07:00:00' , 'event_state' : [0,0,0], 'transition_duration': 1000},
                    {'event_name' :  'Sunrise Phase', 'event_start_time' : '07:30:00', 'event_end_time' : '08:00:00' , 'event_state' : [255,109,0], 'transition_duration': 5000},
                    {'event_name' :  'Alert Phase', 'event_start_time' : '08:00:00', 'event_end_time' : '21:59:00' , 'event_state' : [0,78,103], 'transition_duration': 5000},
                    {'event_name' :  'Relaxation Phase', 'event_start_time' : '22:00:00', 'event_end_time' : '23:59:00' , 'event_state' : [255,27,14], 'transition_duration': 3000},]

# need to work on further transition modes.
valid_transition_modes = ['fade']

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
        self.auto_resume_job = None
        self.queue = Queue.Queue()
        self.state = 'autonomous'
        self.last_ran = {}
        self.led_state = [0,0,0]
        
        # use a running flag for our while loop
        self.run = True
        
        app.logger.debug("Chain_Communicator starting main_loop.")
        self.loop_instance = Thread(target=self.main_loop)
        self.loop_instance.start()
        app.logger.info("Running resume auto, in case were in an auto event.")
        self.resume_auto()
        app.logger.debug("Chain_Communicator init complete.")

    def main_loop(self):
        app.logger.debug("main_loop - processing queue ...")
        while self.run :
            # Grab the next lighting event, block until there is one.
            lighting_event = self.queue.get(block=True)
            # set our chain state
            self.led_chain.set_rgb(lighting_event)
            # write out the previously set state.
            self.led_chain.write()
            # store our state for later comparisons.
            self.led_state = lighting_event

    def auto_transition(self, *args, **kwargs):
        # accepts all events from scheduler, checks if in auto mode, if not throws them away.
        if self.state is 'autonomous':
            self.transition(*args, **kwargs)
        
    def transition(self, state, transition_duration=500, transition_mode='fade'):
        # States must be in the format of a list containing Red, Green and Blue element values in order.
        # example. White = [255,255,255] Red = [255,0,0] Blue = [0,0,255] etc.
        # a duration is represented in an incredibly imprescise unit, known as ticks.  Ticks are executed as fast as the the queue can be processed.
        if transition_mode not in valid_transition_modes:
            raise Exception("Invalid transition mode : %s , valid modes are : %s" % (transition_mode, valid_transition_modes))

        with self.queue.mutex:
            self.queue.queue.clear()
        app.logger.info("Current state is : %s , destination state is : %s , transitioning via %s in : %d ticks" % (self.led_state, state, transition_mode, transition_duration))

        if transition_mode is 'fade':        
            # Using a modified version of http://stackoverflow.com/questions/6455372/smooth-transition-between-two-states-dynamically-generated for smooth transitions between states.
            for transition_count in range(transition_duration - 1):
                event_state = []
                for component in range(3):
                    event_state.append(self.led_state[component] + (state[component] - self.led_state[component]) * transition_count / transition_duration)
                self.queue.put(event_state)

            # last event is always fixed to the destination state to ensure we get there, regardless of any rounding errors. May need to rethink this mechanism, as I suspect small transitions will be prone to rounding errors resulting in a large final jump.
            self.queue.put(state)

    def resume_auto(self):
        # returns system state to autonomous, to be triggered via the scheduler, or via a request hook from the web ui.
        self.state = 'autonomous'
        app.logger.debug("Resume auto called, system state is now : %s" % self.state)
        app.logger.info("Looking to see if current time falls within any events.")
        current_time = datetime.time(datetime.now())
        for event in auto_state_events:
            start_time = datetime.time(datetime.strptime(event['event_start_time'],time_format))
            end_time = datetime.time(datetime.strptime(event['event_end_time'],time_format))
            if current_time > start_time and current_time < end_time:
                app.logger.info("Event : '%s' falls within the current time, executing state." % event['event_name'])
                self.auto_transition(state=event['event_state'])
                break

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

@app.route('/addjob')
def add_job():
    random_state = []
    for i in range(3):
        random_state.append(randint(0,255))
    sched.add_date_job(led_chain.transition, datetime.now() + timedelta(seconds=10), kwargs={'state' : random_state})
    app.logger.debug("Job list now contains : %s" % sched.print_jobs())
    return jsonify({'sucess' : True}) 
    
@app.route('/set/<hex_val>', methods=['GET', 'POST'])
def send_command(hex_val):
    return_object = {'output' : None , 'error' : None, 'success' : False}
    rgb_val = rgb(hex_val)
    app.logger.debug("Given colour_val : %s, converted it to %s" % (hex_val, rgb_val))
    led_chain.transition([rgb_val['R'],rgb_val['G'],rgb_val['B']])
    app.logger.info("Set chain to manual colour state : %s" % [rgb_val['R'],rgb_val['G'],rgb_val['B']])

    # Set our schedulars auto resume time.
    # !!! change this to minutes when finished debugging !!!
    # large gracetime as we want to make sure it fires, regardless of how late it is.
    for job in sched.get_jobs():
        if job.name == 'autoresume':
            app.logger.debug("Removing existing autoresume job, and adding a new one.")
            sched.unschedule_job(led_chain.auto_resume_job)
            led_chain.auto_resume_job = sched.add_date_job(led_chain.resume_auto, datetime.now() + timedelta(minutes=auto_resume_offset), name='autoresume', misfire_grace_time=240)
            break
    else:
        app.logger.debug("No existing autoresume jobs, adding one.")
        led_chain.auto_resume_job = sched.add_date_job(led_chain.resume_auto, datetime.now() + timedelta(minutes=auto_resume_offset), name='autoresume', misfire_grace_time=240)
        
    app.logger.debug("Job list now contains : %s" % sched.print_jobs())
    led_chain.state = 'manual'

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

    sched = Scheduler(config)
    sched.start()
    # calculate our events from the auto_state_events list, need to find a better way of doing this, maybe a config file.
    for event in auto_state_events:
        app.logger.info("Processing scheduled event : %s" % event['event_name'])
        start_hour = event['event_start_time'].split(':')[0]
        start_minute = event['event_start_time'].split(':')[1]
        start_second = event['event_start_time'].split(':')[2]
        start_time = datetime.strptime(event['event_start_time'],time_format)
        end_time = datetime.strptime(event['event_end_time'],time_format)
        event_duration = (end_time - start_time).seconds 
        sched.add_cron_job(led_chain.auto_transition, hour=start_hour, minute=start_minute, second=start_second , name=event['event_name'], kwargs={'state' : event['event_state'], 'transition_duration' : event['transition_duration']}, misfire_grace_time=event_duration)

    app.logger.debug("Startup job list contains : %s" % sched.get_jobs())

    try:
        app.run(host='0.0.0.0', port=8080, use_reloader=False)
    except KeyboardInterrupt:
        app.logger.warning("Caught keyboard interupt.  Shutting down ...")
        led_chain.shutdown()
        sched.shutdown(wait=False)
