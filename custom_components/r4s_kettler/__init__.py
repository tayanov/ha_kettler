"""Support for Redmond Kettler G200S"""
 
#!/usr/local/bin/python3
# coding: utf-8
 
import pexpect
from time import sleep
import time
import colorsys
from datetime import datetime
from textwrap import wrap
import re
from datetime import timedelta
import voluptuous as vol
import logging
 
from homeassistant.core import callback
from homeassistant.const import (CONF_DEVICE, CONF_MAC, CONF_PASSWORD, CONF_SCAN_INTERVAL,)
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType, HomeAssistantType
from homeassistant.helpers.dispatcher import async_dispatcher_send
import homeassistant.util.color as color_util
 
CONF_MIN_TEMP = 40
CONF_MAX_TEMP = 100
CONF_TARGET_TEMP = 100
 
SCAN_INTERVAL = timedelta(seconds=60)
 
REQUIREMENTS = ['pexpect']
 
_LOGGER = logging.getLogger(__name__)
 
SUPPORTED_DOMAINS = ["water_heater", "sensor", "switch"]
 
DOMAIN = "r4s_kettler"
 
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({vol.Required(CONF_DEVICE): cv.string, vol.Required(CONF_MAC): cv.string, vol.Required(CONF_PASSWORD): cv.string, vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL): cv.time_period,})}, extra=vol.ALLOW_EXTRA,)
 
 
 
async def async_setup(hass: HomeAssistantType, config: ConfigType) -> bool:
 
    hass.data[DOMAIN] = {}
 
    kwargs = dict(config[DOMAIN])
    dev = kwargs.get(CONF_DEVICE)
    mac = kwargs.get(CONF_MAC)
    key = kwargs.get(CONF_PASSWORD)
    scan_delta = kwargs.get(CONF_SCAN_INTERVAL)
 
    if len(key) != 16:
        _LOGGER.error("key value is empty or wrong")
        return False
 
    mac_validation = bool(re.match('^' + '[\:\-]'.join(['([0-9a-f]{2})']*6) + '$', mac.lower()))
    if not mac_validation:
        _LOGGER.error("mac value is empty or wrong")
        return False
 
    kettler = hass.data[DOMAIN]["kettler"] = RedmondKettler(hass, mac, key, dev)
    try:
        await kettler.firstConnect()
    except:
        _LOGGER.warning("Connect to Kettler %s through device %s failed", mac, dev)
 
    async_track_time_interval(hass, kettler.async_update, scan_delta)
 
    for platform in SUPPORTED_DOMAINS:
        hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))
 
    return True
 
 
 
class RedmondKettler:
 
    def __init__(self, hass, addr, key, device):
        self.hass = hass
        self._mac = addr
        self._key = key
        self._device = device
        self._mntemp = CONF_MIN_TEMP
        self._mxtemp = CONF_MAX_TEMP
        self._tgtemp = CONF_TARGET_TEMP
        self._temp = 0
        self._time_upd = '00:00'
        self._mode = '00' # '00' - boil, '01' - heat to temp, '03' - backlight
        self._status = '00' #may be '00' - OFF or '02' - ON
        self._iter = 0
        self._connected = False
        self._is_busy = False
        self.child = None
 
 
 
    def theKettlerIsOn(self):
        if self._status == '02':
            if self._mode == '00' or self._mode == '01':
                return True
        return False
 
    def iterase(self): # counter
        self._iter+=1
        if self._iter >= 100: self._iter = 0
 
    def hexToDec(self, chr):
        return int(str(chr), 16)
 
    def decToHex(self, num):
        char = str(hex(int(num))[2:])
        if len(char) < 2:
            char = '0' + char
        return char
 
 
 
    async def async_update(self, now, **kwargs) -> None:
        try:
            await self.modeUpdate()
        except:
            _LOGGER.warning("Update failed")
            return
        async_dispatcher_send(self.hass, DOMAIN)
 
 
 
    def sendResponse(self):
        answ = False
        self.child.sendline("char-write-cmd 0x000c 0100") #send packet to receive messages in future
        self.child.expect(r'\[LE\]>')
        answ = True
        return answ
 
    def sendAuth(self):
        answer = False
        try:
            self.child.sendline("char-write-req 0x000e 55" + self.decToHex(self._iter) + "ff" + self._key + "aa") #send authorise key
            self.child.expect("value: ") # wait for response
            self.child.expect("\r\n") # wait for end string
            connectedStr = self.child.before[0:].decode("utf-8") # answer from device
            answ = connectedStr.split()[3] # parse: 00 - no   01 - yes
            self.child.expect(r'\[LE\]>')
            if answ == '02':
                answer = True
            self.iterase()
        except:
            answer = False
            _LOGGER.error('error auth')
        return answer
 
 
 
    def sendOff(self):
        answ = False
        try:
            self.child.sendline("char-write-req 0x000e 55" + self.decToHex(self._iter) + "04aa") # OFF
#           self.child.expect("value: ")
#           self.child.expect("\r\n")
            self.child.expect(r'\[LE\]>')
            self.iterase()
            answ = True
        except:
            answ = False
            _LOGGER.error('error mode OFF')
        return answ
 
 
 
    def sendStatus(self):
        answ = False
        self.child.sendline("char-write-req 0x000e 55" + self.decToHex(self._iter) + "06aa") # status of device
        self.child.expect("value: ")
        self.child.expect("\r\n")
        statusStr = self.child.before[0:].decode("utf-8") # answer from device example 55 xx 06 00 00 00 00 01 2a 1e 00 00 00 00 00 00 80 00 00 aa
        answer = statusStr.split()
        self._status = str(answer[11])
        self._temp = self.hexToDec(str(answer[8]))
        self._mode = str(answer[3])
        tgtemp = str(answer[5])
        if tgtemp != '00':
            self._tgtemp = self.hexToDec(tgtemp)
        else:
            self._tgtemp = 100
        self.child.expect(r'\[LE\]>')
        self.iterase()
        answ = True
        return answ
 
    def sendMode(self, mode, temp):   # 00 - boil 01 - heat to temp 03 - backlight (boil by default)    temp - in HEX
        answ = False
        self.child.sendline("char-write-req 0x000e 55" + self.decToHex(self._iter) + "05" + mode + "00" + temp + "00aa") # set Properties
#       self.child.expect("value: ")
#       self.child.expect("\r\n")
        self.child.expect(r'\[LE\]>')
        self.iterase()
        answ = True
        return answ
 
 
### composite methods
    def connect(self):
        answ = False
        if self._is_busy:
            self.disconnect()
        try:
            self._is_busy = True
            self.child = pexpect.spawn("gatttool --adapter=" + self._device + " -I -t random -b " + self._mac, ignore_sighup=False)
            self.child.expect(r'\[LE\]>', timeout=1)
            self.child.sendline("connect")
            self.child.expect(r'Connection successful.*\[LE\]>', timeout=1)
            self._is_busy = False
            answ = True
        except:
            _LOGGER.error('error connect')
        return answ
 
    def disconnect(self):
        self._is_busy = True
        if self.child != None:
            self.child.sendline("exit")
        self.child = None
        self._is_busy = False
 
 
 
 
    async def modeOn(self, mode = "00", temp = "00"):
        if not self._is_busy:
            self._is_busy = True
            answ = False
            try:
                if self.connect():
                    if self.sendResponse():
                        if self.sendAuth():
                            if self.sendMode(mode, temp):
                                if self.sendOn():
                                    if self.sendStatus():
                                        answ = True
            except:
                _LOGGER.error('error composite modeOn')
            self.disconnect()
            return answ
        else:
            _LOGGER.info('device is busy now')
            return False
 
 
 
    def sendOn(self):
        answ = False
        try:
            self.child.sendline("char-write-cmd 0x000e 55" + self.decToHex(self._iter) + "03aa") # ON
            self.child.expect(r'\[LE\]>')
            self.iterase()
            answ = True
        except:
            answ = False
            _LOGGER('error mode ON')
        return answ
 
 
    async def modeOff(self):
        if not self._is_busy:
            self._is_busy = True
            answ = False
            try:
                if self.connect():
                    if self.sendResponse():
                        if self.sendAuth():
                            if self.sendOff():
                                if self.sendStatus():
                                    answ = True
            except:
                _LOGGER.error('error composite modeOff')
            self.disconnect()
            return answ
        else:
            _LOGGER.info('device is busy now')
            return False
 
    async def firstConnect(self):
        self._is_busy = True
        iter = 0
        answ = False
        if self.connect():
            while iter < 10: #10 attempts to auth
                answer = False
                if self.sendResponse():
                    if self.sendAuth():
                        answer = True
                        break
                sleep(1)
                iter += 1
            if answer:
                if self.sendStatus():
                    self._time_upd = time.strftime("%H:%M")
                    answ = True
        if answ:
            self._connected = True
            self.disconnect()
 
    async def modeUpdate(self):
        if not self._is_busy:
            self._is_busy = True
            answ = False
            if self.connect():
               if self.sendResponse():
                  if self.sendAuth():
                     if self.sendStatus():
                         self._time_upd = time.strftime("%H:%M")
                         answ = True
            self.disconnect()
            return answ
