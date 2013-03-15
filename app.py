# -*- coding: utf-8 -*-

from __future__ import with_statement
import logging
import time
import json
import Queue
import argparse
import os 
import ConfigParser
 
from logging.handlers import RotatingFileHandler
from random import randint
from threading import Thread
from datetime import datetime, timedelta
from flask import Flask, request, session, url_for, redirect, render_template, abort, g, flash, jsonify
from sys import exit

from apscheduler.scheduler import Scheduler

# Add controlmypi as an optional module.  Can installed via pip
try:
    from controlmypi import ControlMyPi
except ImportError:
    pass


SECRET_KEY = 'nkjfsnkgbkfnge347r28fherg8fskgsd2r3fjkenwkg33f3s'
CONFIGURATION_PATH = "/etc/circadian.conf"

led_chain = None
auto_resume_offset = None # Now set in config file.
auto_resume_job = None

# create our little application
app = Flask(__name__)
app.config.from_object(__name__)
app.debug = True # !!! Set this to False for production use !!!

time_format = "%H:%M:%S"
# Event times should be in the form of %H:%M:%S
# Event states should be in the form of [Red,Green,Blue]
# Event names should be unique, as they are used for last run information
auto_state_events = [{'event_name' :  'Night Phase', 'event_start_time' : '00:00:00', 'event_end_time' : '07:00:00' , 'event_state' : [0,0,0], 'transition_duration': 1000},
                    {'event_name' :  'Sunrise Phase', 'event_start_time' : '07:30:00', 'event_end_time' : '08:59:00' , 'event_state' : [255,109,0], 'transition_duration': 10000},
                    {'event_name' :  'At Work', 'event_start_time' : '09:00:00', 'event_end_time' : '18:59:00' , 'event_state' : [0,0,0], 'transition_duration': 5000},
                    {'event_name' :  'Alert Phase', 'event_start_time' : '19:00:00', 'event_end_time' : '21:59:00' , 'event_state' : [0,78,103], 'transition_duration': 5000},
                    {'event_name' :  'Relaxation Phase', 'event_start_time' : '22:00:00', 'event_end_time' : '23:59:00' , 'event_state' : [255,27,14], 'transition_duration': 3000},]

# need to work on further transition modes.
valid_transition_modes = ['fade']
# Currently supported led drivers 
valid_led_drivers = ['ws2801','lpd6803','lpd8806']

# Stolen from http://stackoverflow.com/questions/4296249/how-do-i-convert-a-hex-triplet-to-an-rgb-tuple-and-back
HEX = '0123456789abcdef'
HEX2 = dict((a+b, HEX.index(a)*16 + HEX.index(b)) for a in HEX for b in HEX)

def rgb(triplet):
    triplet = triplet.lower()
    return { 'R' : HEX2[triplet[0:2]], 'G' : HEX2[triplet[2:4]], 'B' : HEX2[triplet[4:6]]}

def triplet(rgb):
    return format((rgb[0]<<16)|(rgb[1]<<8)|rgb[2], '06x')


class Chain_Communicator:
    
    def __init__(self, driver_type, chain_length, controlmypi=None):
        if driver_type == 'ws2801':
            from pigredients.ics import ws2801 as ws2801
            self.led_chain = ws2801.WS2801_Chain(ics_in_chain=chain_length)
        elif driver_type == 'lpd6803':
            from pigredients.ics import lpd6803 as lpd6803
            self.led_chain = lpd6803.LPD6803_Chain(ics_in_chain=chain_length)
        elif driver_type == 'lpd8806':
            from pigredients.ics import lpd8806 as lpd8806
            self.led_chain = lpd8806.LPD8806_Chain(ics_in_chain=chain_length)
        self.auto_resume_job = None
        self.queue = Queue.Queue()
        self.mode_jobs = []
        self.state = 'autonomous'
        self.led_state = [0,0,0]
        self.controlmypi = controlmypi
        
        # use a running flag for our while loop
        self.run = True
        
        app.logger.debug("Chain_Communicator starting main_loop.")
        self.loop_instance = Thread(target=self.main_loop)
        self.loop_instance.start()
        app.logger.info("Running resume auto, in case were in an auto event.")
        self.resume_auto()
        app.logger.debug("Chain_Communicator init complete.")

    def main_loop(self):
        try:
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
        except KeyboardInterrupt:
            self.run = False
            app.logger.warning("Caught keyboard interupt in main_loop.  Shutting down ...")

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
            if self.controlmypi is not None:
                app.logger.debug("Updating control my pi with state : #%s" % triplet(state))
                self.controlmypi.update_status({'indicator': "#%s" % triplet(state)})

    def clear_mode(self):
        app.logger.debug("Removing any mode jobs from queue")
        for job in self.mode_jobs:
            app.logger.debug("Removing existing mode job")
            sched.unschedule_job(job)
        self.mode_jobs = []

    def resume_auto(self):
        if self.state is not 'manual':
            self.clear_mode()
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
        self.run = False
        # send final state to avoid blocking on queue.
        self.queue.put([0,0,0])
        self.loop_instance.join()



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

@app.route('/mode/auto')
def auto_mode():
    if led_chain.state is not 'autonomous':
        led_chain.resume_auto()
    
    return jsonify({'sucess' : True})


@app.route('/mode/cycle')
def cycle_mode():
    if led_chain.state is not 'cycle':
        led_chain.state = 'cycle'
        # Schedule our cycle events ...
        led_chain.mode_jobs = []
        led_chain.mode_jobs.append(sched.add_interval_job(led_chain.transition, seconds=40, start_date=datetime.now() + timedelta(seconds=1), name='__cycle_0', kwargs={'state' : [126,0,255], 'transition_duration' : 800}))
        led_chain.mode_jobs.append(sched.add_interval_job(led_chain.transition, seconds=40, start_date=datetime.now() + timedelta(seconds=6), name='__cycle_1', kwargs={'state' : [255,0,188], 'transition_duration' : 800}))
        led_chain.mode_jobs.append(sched.add_interval_job(led_chain.transition, seconds=40, start_date=datetime.now() + timedelta(seconds=11), name='__cycle_2', kwargs={'state' : [255,0,0], 'transition_duration' : 800}))
        led_chain.mode_jobs.append(sched.add_interval_job(led_chain.transition, seconds=40, start_date=datetime.now() + timedelta(seconds=16), name='__cycle_3', kwargs={'state' : [255,197,0], 'transition_duration' : 800}))
        led_chain.mode_jobs.append(sched.add_interval_job(led_chain.transition, seconds=40, start_date=datetime.now() + timedelta(seconds=21), name='__cycle_4', kwargs={'state' : [135,255,0], 'transition_duration' : 800}))
        led_chain.mode_jobs.append(sched.add_interval_job(led_chain.transition, seconds=40, start_date=datetime.now() + timedelta(seconds=26), name='__cycle_5', kwargs={'state' : [0,255,34], 'transition_duration' : 800}))
        led_chain.mode_jobs.append(sched.add_interval_job(led_chain.transition, seconds=40, start_date=datetime.now() + timedelta(seconds=31), name='__cycle_6', kwargs={'state' : [0,255,254], 'transition_duration' : 800}))
        led_chain.mode_jobs.append(sched.add_interval_job(led_chain.transition, seconds=40, start_date=datetime.now() + timedelta(seconds=36), name='__cycle_7', kwargs={'state' : [0,52,255], 'transition_duration' : 800}))

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

    return jsonify({'sucess' : True})
    


@app.route('/job/list')
def list_jobs():
    print sched.get_jobs()
    job_list = {}
    for job in sched.get_jobs():
        # Filter our our internal jobs
        if not job.name.startswith('__'):
            job_list[job.name] = {}
            print "Job trigger : %s type : %s" % (job.trigger, type(job.trigger))
            job_list[job.name]['type'] = 'stuff'
    return jsonify(job_list) 

@app.route('/job/delete')
def delete_job():
    return jsonify({'sucess' : False}) 


@app.route('/job/date/add')
def add_date_job():
    random_state = []
    for i in range(3):
        random_state.append(randint(0,255))
    sched.add_date_job(led_chain.transition, datetime.now() + timedelta(seconds=10), kwargs={'state' : random_state})
    app.logger.debug("Job list now contains : %s" % sched.print_jobs())
    return jsonify({'sucess' : True}) 

@app.route('/job/cron/add')
def add_cron_job():
    return jsonify({'sucess' : False}) 

@app.route('/job/interval/add')
def add_interval_job():
    return jsonify({'sucess' : False}) 



@app.route('/get/current_state')
def get_state():
    return jsonify({'state': "#%s" % triplet(led_chain.led_state)})

@app.route('/get/current_mode')
def get_mode():
    return jsonify({'mode': "%s" % led_chain.state})

    
def manual_set(hex_val):
    if led_chain.state is not 'manual':
        led_chain.clear_mode()

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
    return return_object
    
@app.route('/set/<hex_val>', methods=['GET', 'POST'])
def send_command(hex_val):
    return jsonify(manual_set(hex_val))

def on_controlmypi_msg(connection, key, value):
    app.logger.debug("Recieved message from control my pi, key : %s , value : %s" % (key, value))
    if key == 'wheel':
        manual_set(value[1:7])
        connection.update_status({'indicator':value})

# add some filters to jinja
app.jinja_env.filters['datetimeformat'] = format_datetime


if __name__ == '__main__':
    logging.basicConfig()
    app_config = ConfigParser.SafeConfigParser()
    app_config.readfp(open(CONFIGURATION_PATH))
    
    # create console handler and set level to debug, with auto log rotate max size 10mb keeping 10 logs.
    file_handler = RotatingFileHandler( app_config.get("general", "logging_path") , maxBytes=10240000, backupCount=10)

    # create formatter
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    # add formatter to our console handler
    file_handler.setFormatter(log_formatter)

    file_handler.setLevel(logging.DEBUG)
    app.logger.addHandler(file_handler)

    try:
        auto_resume_offset = int(app_config.get("behaviour", "auto_resume_delay"))
    except ConfigParser.NoOptionError:
        app.logger.warning("No 'auto_resume_delay' option specified in 'behaviour' section of the config file, defaulting to 90")
        auto_resume_offset = 90  

    parser = argparse.ArgumentParser(description='Circadian and Mood Lighting.')

    parser.add_argument('--type', action="store", dest="driver_type", required=False, help='The model number of the LED driver, eg. ws2801 or lpd6803. defaults to configuration file.')
    parser.add_argument('--length', action="store", dest="led_count", type=int, required=False, help='The number of LEDs in the chain. defaults to configuration file.')
    parser.add_argument("--controlmypi", help="Make available from controlmypi.com", dest="control_my_pi", action="store_true", default=False)
    args = parser.parse_args()


    if args.control_my_pi:
        app.logger.debug("Running up Control My Pi connection.")
        p = [ [ ['L','Pick a colour:'],['W','wheel'],['L','Current lighting State:'],['I','indicator','#000000'] ] ]
        controlmypi_connection = ControlMyPi(app_config.get("controlmypi", "jabber_id"), app_config.get("controlmypi", "password"), app_config.get("controlmypi", "panel_id"), app_config.get("controlmypi", "panel_name"), p, on_controlmypi_msg)
    else:
        controlmypi_connection = None


    if args.driver_type is not None and args.led_count is not None:
        if args.driver_type.lower() in valid_led_drivers:
            app.logger.info("LED Driver is :%s with %d in the chain" % (args.driver_type, args.led_count))
            led_chain = Chain_Communicator(driver_type=args.driver_type.lower(), chain_length=args.led_count, controlmypi=controlmypi_connection)
        else:
            raise Exception("Invalid LED Driver %s specified, implemented types are : %s" % (args.driver_type, valid_led_drivers))
    else:
        try:
            led_chain = Chain_Communicator(driver_type=app_config.get("chain", "type"), chain_length=int(app_config.get("chain", "length")), controlmypi=controlmypi_connection)
        except ConfigParser.NoOptionError:
            app.logger.warning("Unable to find both length and type properties in chain section of configuration file.")

    sched = Scheduler()
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

    if args.control_my_pi:
        if controlmypi_connection.start_control():
            try:
                app.run(host='0.0.0.0', port=int(app_config.get("general", "web_port")), use_reloader=False)
            except KeyboardInterrupt:
                app.logger.warning("Caught keyboard interupt.  Shutting down ...")
        app.logger.info("Calling shutdown on ControlMyPi.com")
        controlmypi_connection.stop_control()
    else:
        try:
            app.run(host='0.0.0.0', port=int(app_config.get("general", "web_port")), use_reloader=False)
        except KeyboardInterrupt:
            app.logger.warning("Caught keyboard interupt.  Shutting down ...")


    app.logger.info("Calling shutdown on led chain")
    led_chain.shutdown()
    app.logger.info("Calling shutdown on scheduler")
    sched.shutdown(wait=False)
    app.logger.info("Shutting down logger and exiting ...")
    logging.shutdown()
    exit(0)
