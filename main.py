# MIT License (MIT)
# Copyright (c) 2023 Stephen Carey
# https://opensource.org/licenses/MIT

import _thread
import json
import sys
import time

import ds18x20
import ntptime
import onewire
import uasyncio as asyncio
import utime
from machine import Pin

# https://github.com/lorcap/upy-micropython-lib/blob/datetime/python-stdlib/datetime/datetime.py
from datetime import datetime
import mqtt_local
from mqtt_as import MQTTClient
from config import BASE_TOPIC

AVAILABLE_TOPIC = f'{BASE_TOPIC}/availability'
ERROR_TOPIC = f'{BASE_TOPIC}/error'
CONFIG_TOPIC = f'{BASE_TOPIC}/config'
STATE_TOPIC = f'{BASE_TOPIC}/status'

heat_relay_pin = Pin(18, Pin.OUT, value=0)
cool_relay_pin = Pin(19, Pin.OUT, value=0)
temp_pin = Pin(21)

# OneWire, DallasTemperature
ds = ds18x20.DS18X20(onewire.OneWire(temp_pin))

# scan for devices on the bus
roms = ds.scan()
print('found devices:', roms)

client = None

previous_temp = 0.0
config_done = False
minimum_off_mins = 3
initial_ntp_success = False
schedule_start_time = None
schedule_adjustment = 0
base_low_temp = 0
base_high_temp = 0
cooling_off_count = 0
cooling_state = 'off'
schedule_adjust_days = []
schedule_adjust_temp = []
publish_data = {"currentTemp": 0, "lowTempLimit": None, "highTempLimit": None, "tempUnit": "F", "heatOn": False,
                "coolOn": False}


def parse_schedule(schedule):
    if schedule:
        schedule_adjust_days.clear()
        schedule_adjust_temp.clear()
        for change in schedule:
            # print(f'days: {change["daysLater"]}, tempChange: {change["tempChange"]}')
            schedule_adjust_days.append(change['daysLater'])
            schedule_adjust_temp.append(change['tempChange'])


async def determine_temp_adjustment(days_in):
    global schedule_adjustment
    adjustment = 0
    for i in range(len(schedule_adjust_days)):
        if schedule_adjust_days[i] <= days_in:
            adjustment = schedule_adjust_temp[i]
    schedule_adjustment = adjustment
    publish_data['lowTempLimit'] = base_low_temp + adjustment
    publish_data['highTempLimit'] = base_high_temp + adjustment


def get_now_datetime():
    #  I had trouble datetime.now(timezone.utc).  This is ugly but it works.
    now = time.gmtime(time.time())
    return datetime(now[0], now[1], now[2], now[3], now[4], now[5])


def handle_incoming_message(topic, msg, retained):
    msg_string = str(msg, 'UTF-8')
    topic_string = str(topic, 'UTF-8')
    print(f'{topic_string}: {msg_string}')
    if topic_string == CONFIG_TOPIC:
        global config_done, schedule_start_time, base_low_temp, base_high_temp, minimum_off_mins
        try:
            config = json.loads(msg_string)
            # 2023-06-04T12:00:00
            schedule_start_time = datetime.fromisoformat(config.get('startTimeUTC'))

            base_low_temp = config.get('lowTempLimit')
            base_high_temp = config.get('highTempLimit')
            minimum_off_mins = config.get('minimumOffMins')

            publish_data['tempUnit'] = 'C' if config.get('celsius') else 'F'
            parse_schedule(config.get('tempChanges'))
            config_done = True
        except Exception as e:
            print(f'Problem with config: {e}')
            sys.print_exception(e)


async def wifi_han(state):
    print('Wifi is ', 'up' if state else 'down')
    await asyncio.sleep(1)


# If you connect with clean_session True, must re-subscribe (MQTT spec 3.1.2.4)
async def conn_han(client):
    await client.subscribe(CONFIG_TOPIC, 0)
    await online()


async def online():
    global initial_wifi_up
    initial_wifi_up = True
    await client.publish(AVAILABLE_TOPIC, 'online', retain=True, qos=0)


async def safe_publish(topic, message, retain=True):
    # we don't want problems publishing to interfere with the temperature processing.  Maybe add an LED to indicate
    # broker issues?
    try:
        await client.publish(topic, message, retain=retain)
    except Exception as e:
        print("Problem pulishing message: {e}")


async def main():
    await client.connect()
    await asyncio.sleep(2)  # Give broker time
    await online()
    while True:
        while not config_done:
            print("Config not found, will check again in a few secs...")
            await asyncio.sleep(5)

        while not initial_ntp_success:
            print("Waiting for ntp, will check again in a few secs...")
            await asyncio.sleep(5)

        try:
            ds.convert_temp()
            asyncio.sleep_ms(750)
            current_temp = ds.read_temp(roms[0])
            if publish_data['tempUnit'] == 'F':
                current_temp = (current_temp * 1.8) + 32
            print(f'Temp = {current_temp}{publish_data["tempUnit"]}')
            publish_data['currentTemp'] = current_temp

            global previous_temp
            if previous_temp == 0.0:
                previous_temp = current_temp

            now = get_now_datetime()
            days_in = (now - schedule_start_time).days
            if days_in < 0:
                # start was in the future?  NTP is messed up?
                error_str = f'Now is {now}, startTime is {schedule_start_time} UTC, diff was {days_in} days.'
                print(error_str)
                await safe_publish(ERROR_TOPIC, error_str)
            else:
                await determine_temp_adjustment(days_in)
            print(
                f'{days_in} day(s) in, adjustment is {schedule_adjustment} degrees, range is {publish_data["lowTempLimit"]}-{publish_data["highTempLimit"]}.')

            if base_low_temp and ((current_temp + previous_temp) / 2) < publish_data["lowTempLimit"]:
                print("Heating is active")
                heat_relay_pin.on()
            else:
                heat_relay_pin.off()

            global cooling_off_count, cooling_state
            if base_high_temp and ((current_temp + previous_temp) / 2) > publish_data[
                'highTempLimit'] and cooling_state != 'waiting':
                print("Cooling is active")
                cool_relay_pin.on()
                cooling_state = 'on'
            else:
                cool_relay_pin.off()
                if cooling_state == 'on':
                    cooling_state = 'waiting'
                    cooling_off_count = 0
                    print(f"Cooling just turned off, beginning {minimum_off_mins} minute cooldown period")
                elif cooling_state == 'waiting':
                    cooling_off_count += 1
                    print(f"Cooldown minute {cooling_off_count} passed...")
                    if cooling_off_count >= minimum_off_mins - 1:
                        cooling_state = 'off'
                        print(f"Cooldown period finished, cooling available next cycle.")
            previous_temp = current_temp

            publish_data['coolOn'] = cool_relay_pin.value()
            publish_data['heatOn'] = heat_relay_pin.value()
            await safe_publish(STATE_TOPIC, json.dumps(publish_data))
        except Exception as e:
            print("Problem in temp loop.")
            sys.print_exception(e)
            await safe_publish(ERROR_TOPIC, f'Problem in main loop: {e}.')
        finally:
            await asyncio.sleep(60)


def update_clock_thread():
    global initial_ntp_success
    while True:
        try:
            while not config_done:
                print("NTP thread waiting for config...")
                utime.sleep(1)
            ntptime.settime()
            initial_ntp_success = True
            print(f"Time udpated to {utime.gmtime(utime.time())}")
        except OSError as o:
            print("Problem updating time, will try again...")
            sys.print_exception(o)
        if not initial_ntp_success:
            utime.sleep(2)  # try again more quickly since wifi may not have been connected yet
        else:
            utime.sleep(600)


mqtt_local.config['subs_cb'] = handle_incoming_message
mqtt_local.config['connect_coro'] = conn_han
mqtt_local.config['wifi_coro'] = wifi_han
mqtt_local.config['will'] = [AVAILABLE_TOPIC, 'offline', True, 0]

MQTTClient.DEBUG = False
client = MQTTClient(mqtt_local.config)

try:
    _thread.start_new_thread(update_clock_thread, ())

    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
finally:
    client.close()
    asyncio.stop()
