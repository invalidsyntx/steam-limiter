#!/usr/bin/env python
#
# Copyright (C) 2011 Nigel Bree
# All Rights Reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

#-----------------------------------------------------------------------------

# This application mainly provides a simple way for installations of the Steam
# Limiter application from http://steam-limiter.googlecode.com to determine
# whether an updated version of the application has been made available. That's
# just a simple matter of retrieving some data from a URL to retrieve the
# current version number and a download URL for the latest installer.
#
# Given the way that web access to the source repository in Google Code works
# in principle that could be used instead, but there are a couple of advantages
# to having a service like this instead, in addition to the fact that as with
# the NSIS installer for the limiter client it's a handy example of how to do
# such things.
#
# For instance, an additional thing I could add to this is to have an installer
# extension for the limiter client app which can retrieve the client's real IP
# and thus suggest to it the ideal server (or server list) to set as the filter
# default, instead of assuming TelstraClear - TC is the default ISP at present
# since that's my ISP, but the filter app is usable by other New Zealand ISPs
# and it would be nice to be able to make that seamless.
#
# The tradeoff there, however, is that to learn what the external IP ranges for
# ISPs are takes some effort, and really the easiest way to do that is to log
# them (even if it's just to the Python log for me to cast an eyeball over, not
# to a datastore); similarly, it's good for the update check to not just be a
# static page.
#

import jinja2
import os
import logging
import webapp2
import json

from google.appengine.ext.webapp import template
from google.appengine.ext import db
from google.appengine.api import users, xmpp, mail

# These will most likely eventually become some datastore items in future, but
# making them static will do just to start the update support off.

code_ui_base = 'http://code.google.com/p/steam-limiter/'
code_file_base = 'http://steam-limiter.googlecode.com/files/'

latest_version = '0.6.1.0'
latest_file = 'steamlimit-' + latest_version + '.exe'

# If I want to map source IPs to ISP names, you'd normally just do some kind of
# reverse DNS lookup: however, the API for that is blocked by Google App Engine
# and there's no reliable way to bypass it, see
#   http://code.google.com/p/googleappengine/issues/detail?id=354
#
# However, most (paid) GeoIP databases include this data too, although most are
# asking crazy prices. There's even a more-or-less free CSV database for that
# which can be obtained from MaxMind, Inc who sell the full databases, since for
# our purposes the ASN data should be all we need:
#   http://www.maxmind.com/app/asnum
# A suitable Python API for working with this is 
#   http://code.google.com/p/pygeoip/
#
# However, what I've done for simplicity for now is rather than putting the IP
# mappings into the datastore is to prepare a file with a suitable Python data
# literal I can import, such with the subset of IP ranges I care about.

import ip_match
import string

# Assist the mapping process by converting the IP address string into a number

def stringip_to_number (text):
    fields = string.split (text, '.')
    total = 0
    for item in fields:
        total = (total << 8) + int (item)

    return total;

# Alternate mapping table for IPv6 netblocks; currently there are few of these,
# but Internode in Australia appear to be one such organization. For now I'm
# not bothering to explicitly represent the prefix length, I'm just using a
# simple prefix match.

ipv6_prefixes = {
    "2001:4400:": 0,    # TelstraClear New Zealand
    "2001:4478:": 12,   # iiNet Australia
    "2001:4479:": 12,   # iiNet Australia
    "2001:44B8:": 11,   # Internode Australia
    "2406:E000:": 2	# Snap! New Zealand
};

# Find any matching tuple inside the ip_match.ip_table list, which is sorted,
# and return the ISP number for it
#
# The remapping for loopback below is to help testing; since we don't have a
# real IP to use in the local GAE dev environment, try the various known Steam
# server IPs used by different ISPs to see if we can identify their netblocks.

def find_netblock (ip):
    if ip == '127.0.0.1':
        ip = '203.167.129.4'

    ipType = type (ip)
    if ipType == str or ipType == unicode:
        if ':' in ip:   # look in IPv6 table.
            ip = ip.upper ()
            for prefix in ipv6_prefixes.items ():
                if ip.startswith (prefix [0]):
                    return prefix [1]

            logging.warning ('Unknown mapping for IPv6 address ' + ip)
            return - 1

        ipv4 = stringip_to_number (ip)
    else:
        ipv4 = ip

    # Binary search or linear? Now the table is 1200 long, it's worth it to do
    # a binary search.

    table = ip_match.ip_table
    low = 0
    high = len (table) - 1

    while low <= high:
        mid = (low + high) / 2
        item = table [mid]

        if item [0] >= ipv4:
            # Move down
            high = mid - 1
            continue

        if item [1] >= ipv4:
            # We have a match
            return item [2]

        # Move up
        low = mid + 1

    if type (ip) == str:
        logging.warning ('Unknown mapping for IPv4 address ' + ip)

    return - 1


# The ISP indexes I use in the netblock table
#
# Here I prefer to use DNS names in the new-style filter rules, since it
# should save on maintenance work; even for filtered servers the reverse
# lookup tends to be available through DNS so only those cases where there
# is no known standard name have I kept a raw IP.
#
# [ At least on the Australian side, the gamearena.com.au IP assignments have
#   changed over time, but they have correctly kept the DNS names stable as
#   they should, so theory and practice seem to be in accord. ]
#
# I can actually use DNS names for the 'server' item, but for the NZ ISPs I
# didn't to start with so I'm being consistent out of habit; there's going to
# be some complexity in the monitor app to upgrade rule styles so that the
# webservice ones eventually get preferred, and to make running a redetect
# easier (related to a feature request for home+away locations for LAN parties
# and the like).
#
# The suggested rules here are somewhat of a stab in the dark; I initially
# tried my own, but Angus Wolfcastle has posted a set in an attachment at
# http://www.anguswolfcastle.co.cc/steam-1/filtering-methods - his will be
# way better than anything I could do and since he's written that I'll
# incorporate them right away for the benefit of the Australian folks.
#
# My intuition is that for most Australian users able to use Steam Manager
# (i.e., not running Windows XP) it's a better choice than Steam Limiter in
# any case because it's less prescriptive and as long as the IP list is long
# enough the the Steam client can still exert a degree of freedom.
#
# Something powerful Steam Limiter can potentially do in future versions is
# actually *measure* ping times to the servers in the filter list; making a
# robust system for automatically managing the ideal server selection at an
# even finer level than Valve do, and removing most of the need for manual
# rule curation other than reports of new unmetered servers to add to the
# rotation for monitorin). However, that depends on having a pool of users
# willing to opt in to anonymously contributing that data, which I cannot
# see occurring in the near future.

isps = {
    - 1: { 'name': 'Unknown', 'server': '203.167.129.4',
           'filter': '# No specific content server for your ISP' },

    # Note that most NZ Universities appear to have peering with and/or student
    # internet provided via Snap! - most I've lumped in as part of Snap! but
    # Waikato is a special case having an old netblock with a full class B and
    # it is being set as its own case, just using the same rules as Snap! for
    # now. I'll call it Lightstream (which is a semi-commercial spinoff used
    # for student internet) since that's probably most useful.

    # Note that aside from most NZ ISPs not generally understanding the concept
    # of giving things like servers DNS names, pretty much all of these are
    # filtered so I can't detect whether they support port 80 or not, and none
    # of the ISPs document this properly.

    0: { 'name': 'TelstraClear New Zealand', 'server': '203.167.129.4',
         'filter': '*:27030=wlgwpstmcon01.telstraclear.co.nz' },
    1: { 'name': 'Orcon New Zealand', 'server': '0.0.0.0',
         'filter': '# Orcon no longer have a Steam server' },
    2: { 'name': 'Snap! New Zealand', 'server': '0.0.0.0',
         'filter': '# Snap! no longer have a Steam server' },
    3: { 'name': 'Slingshot New Zealand', 'server': '119.224.142.146',
         'filter': '*:27030=119.224.142.146' },
    4: { 'name': 'Lightstream, Waikato New Zealand', 'server': '202.124.127.66',
         'filter': '*:27030=202.124.127.66' },
    5: { 'name': 'Xnet/WorldxChange New Zealand', 'server': '58.28.25.146',
         'filter': '*:27030=58.28.25.146' },
    6: { 'name': 'ACSData, Wellington NZ', 'server': '0.0.0.0',
         'filter': '# No known unmetered Steam server' },
    7: { 'name': 'Vodafone New Zealand', 'server': '0.0.0.0',
         'filter': '# No known unmetered Steam server' },
    8: { 'name': 'Telecom/XTRA New Zealand', 'server': '0.0.0.0',
         'filter': '# No known unmetered Steam server' },
    9: { 'name': 'InSPire New Zealand', 'server': '0.0.0.0',
         'filter': '# Please contribute a server IP for InSPire' },

    # For the Australian ISPs I'm using two servers per ISP to start but the
    # ideal lists here are a bit hard to figure, since there are a mix of
    # filtered and non-filtered servers, and thanks to peering often customers
    # of one ISP can get optimal service from a different peer.
    # It's hard to tell whether Westnet's netblock really should be merged into
    # the iiNet one or not, but I'm keeping that separate for now.

    # Telstra's GameArena servers used to be be classic Filtered servers with
    # no visible HTTP support. This has changed as of this announcement:
    # http://forums.gamearena.com.au/suggestionsfeedback/topic/159928-new-steam-content-system-now-available-on-gamearena
    # in which supposedly 203.39.198.136 is now the only unmetered server
    # (it's hard to make sense of that thread since there's conflicting
    # information there and you can't tell who is official Telstra and
    # who's just some random person).

    10: { 'name': 'Telstra BigPond Australia', 'server': '0.0.0.0',
          'filter': '*:27030=203.39.198.136;' +
                    'content?.steampowered.com=203.39.198.136' },

    # For a long time the iiNet rule was only these three specific servers:
    # *:27030=steam1.filearena.net,steam-wa.3fl.net.au,steam-nsw.3fl.net.au
    #
    # Internode's and iiNet's rules appear to be broken. Supposedly all the
    # iiNet steam content is on steam.cdn.on.net but that does not appear to
    # work for me or some Internode customers. The only two servers on the
    # Internode master list - the only list they provide, there is no list
    # of just Steam servers -
    # http://www.internode.on.net/residential/entertainment/unmetered_content/ip_address_list/
    # that I can confirm *are* steam servers are 49.143.234.14 and
    # files-oc-syd.games.on.net - those are the only individual IPs on that
    # list that appear to be steam content servers.  There may be more, but
    # if so they are just undocumented entries in some of the larger netblocks
    # listed on that page and I can't scan them all by hand.

    11: { 'name': 'Internode Australia', 'server': '0.0.0.0',
          'filter': '*:27030=49.143.234.14,files-oc-syd.games.on.net;' +
                    'content?.steampowered.com=49.143.234.14,files-oc-syd.games.on.net',
          'allow': '//steam.cdn.on.net=*' },

    # iiNet are now special, because their Steam server (which is behind a
    # front-end like the Telstra one) returns 403 access errors to me but is
    # accessible to subscribers, and it appears to want its host: entry to be
    # itself and rejects the *.steampowered.com domain names flat out.

    12: { 'name': 'iiNet Australia', 'server': '0.0.0.0',
          'filter': '*:27030=steam.cdn.on.net;' +
                    'content?.steampowered.com=',
          'allow': '//steam.cdn.on.net=*' },

    # Evidently Optus actually don't actually offer any unmetered content, so
    # these server selections are intended more for download performance than
    # for providing unmetered data. I've a report from an Optus customer that
    # the Sydney and San Jose servers give much better perf than the two which
    # were previously listed here. The 49.xx.xx.xx servers are on AS209 QWest
    # so are in the United States, so the Sydney server should work well.
    #
    # The hard bit about this is that performance depends on load, and so the
    # server selections that work well most of the time may end up being less
    # than optimal during load spikes such as Steam sales.
    #
    # Whether I should try and shoot down steam.ix.asn.au here via DNS is not
    # clear, but for safety I will try valve.tge2-3.fr4.syd.llnw.net since that
    # is at least unmetered (at worst it'll 404 due to virtual hosting). Since
    # Optus don't support on.net either, I'll do that same for that.

    13: { 'name': 'Optus Australia', 'server': '0.0.0.0',
          'filter': '*:27030=49.143.234.6,49.143.234.14;' +
                    'content?.steampowered.com=49.143.234.6,49.143.234.14'  },

    # Angus Wolfcastle pointed out http://www.ipgn.com.au/Support/Support/Steam
    # where iPrimus list their unmetered servers. That page has now gone and I
    # have no information on iPrimus now, and as with iiNet and Internode most
    # of the old Steam servers are now completely gone.

    14: { 'name': 'iPrimus Australia', 'server': '0.0.0.0',
          'filter': '*:27030=49.143.234.14,files-oc-syd.games.on.net;' +
                    'content?.steampowered.com=49.143.234.14,files-oc-syd.games.on.net',
          'allow': '//steam.cdn.on.net=*' },

    # As with iPrimus since many of the old Steam servers listed as unmetered
    # are now no longer active, try using the current Internode rules

    15: { 'name': 'Westnet Internet Services (Perth, WA)', 'server': '0.0.0.0',
          'filter': '*:27030=49.143.234.14,files-oc-syd.games.on.net;' +
                    'content?.steampowered.com=49.143.234.14,files-oc-syd.games.on.net',
          'allow': '//steam.cdn.on.net=*' },

    # Adam appear to have a list of servers (unfortunately, not DNS names and also
    # unfortunately, no indication which ones serve HTTP content).
    # http://www.adam.com.au/support/downloads/unmetered_ip_address_list.txt
    # The on.net Steam servers are also generally claimed as unmetered, but not
    # the WAIX ones which Valve's CDN will sometimes try and force so that needs
    # to be redirected to steam.cdn.on.net through DNS to steam.cdn.on.net, as
    # that fortunately isn't sensitive to the Host: presented (thanks to WP user
    # networkMe for his immense help in diagnosing all this).

    16: { 'name': 'Adam Internet (Adelaide, SA)', 'server': '0.0.0.0',
          'filter': '*:27030=49.143.234.14,files-oc-syd.games.on.net;' +
                    'content?.steampowered.com=49.143.234.14,files-oc-syd.games.on.net',
          'allow': '//steam.cdn.on.net=*' },

    17: { 'name': 'EAccess Broadband, Australia', 'server': '0.0.0.0',
          'filter': '# No known unmetered Steam server' },

    # Slots 18-29 are reserved for future Australian ISPs or tertiary institutions.

    # Because it seems customers with dual ISP accounts is common in South
    # Africa (along with a large fraction of the retail ISPs being pure
    # resellers), detection in ZA needs extra work from the client side to
    # be sure of what connectivity is present, so there are rule extensions
    # to detect dual-ISP situations and prefer the WebAfrica unmetered server
    # if there's connectivity to the WebAfrica customer side.

    30: { 'name': 'Internet Solutions (Johannesburg, South Africa)', 'server': '196.38.180.3',
          'filter': '*:27030=steam.isgaming.co.za',
          'test': {
              'report': True,
              'steam.wa.co.za icmp *.wa.co.za': {
                  0: {
                      'ispname': 'WebAfrica/IS dual ISP',
                      'filterrule': '*:27030=steam.wa.co.za,steam2.wa.co.za;content?.steampowered.com=steam.wa.co.za,steam2.wa.co.za'
                  }
              }
          }
        },
    31: { 'name': 'webafrica (Cape Town, South Africa)', 'server': '41.185.24.21',
          'filter': '*:27030=steam.wa.co.za,steam2.wa.co.za;content?.steampowered.com=steam.wa.co.za,steam2.wa.co.za' },
    32: { 'name': 'Telkom SAIX, South Africa', 'server': '0.0.0.0',
          'filter': '# No known unmetered Steam server',
          'test': {
              'report': True,
              'steam.wa.co.za icmp *.wa.co.za': {
                  0: {
                      'ispname': 'WebAfrica/SAIX dual ISP',
                      'filterrule': '*:27030=steam.wa.co.za,steam2.wa.co.za;content?.steampowered.com=steam.wa.co.za,steam2.wa.co.za'
                  }
              }
          }
        },
    33: { 'name': 'MWeb, South Africa', 'server': '196.28.69.201',
          'filter': '*:27030=196.28.69.201,196.28.169.201',
          'test': {
              'report': True,
              'steam.wa.co.za icmp *.wa.co.za': {
                  0: {
                      'ispname': 'WebAfrica/MWeb dual ISP',
                      'filterrule': '*:27030=steam.wa.co.za,steam2.wa.co.za;content?.steampowered.com=steam.wa.co.za,steam2.wa.co.za'
                  }
              }
          }
        },
    34: { 'name': 'Cybersmart, South Africa', 'server': '0.0.0.0',
          'filter': '# No known Steam server for Cybersmart',
          'test': {
              'report': True,
              'steam.wa.co.za icmp *.wa.co.za': {
                  0: {
                      'ispname': 'WebAfrica/Cybersmart dual ISP',
                      'filterrule': '*:27030=steam.wa.co.za,steam2.wa.co.za;content?.steampowered.com=steam.wa.co.za,steam2.wa.co.za'
                  }
              }
          }
        },

    # Slots 35-39 are reserved for future South African ISPs

    # Iceland is tricky; like a lot of countries, there's a former state
    # monopoly which has a lot of DSL users, but they don't run a Steam server.
    # Only Vodafone do now and it's filtered so I can't easily find out whether
    # it has HTTP support enabled, hence the filter test.

    40: { 'name': 'Vodafone Iceland', 'server': '193.4.194.101',
          'filter': '*:27030=193.4.194.101',
          'test': {
              'report': True,
              '193.4.194.101 80': {
                  0: {
                      'filterrule': '*:27030=193.4.194.101;content?.steampowered.com=193.4.194.101'
                  }
              }
          }
        },

    # Regularly installs turn up from Google netblocks; possibly this is part
    # of sandboxed malware scanning of Google Code downloads, but equally for
    # all I know it could be humans, possibly in the Sydney office where they
    # develop Google Maps.

    50: { 'name': 'Google, Inc', 'server': '0.0.0.0',
          'filter': '# What Steam server do Google use...?' },

    # I really have no idea what's going on with installs from Comcast netblocks
    # so I'd hope one day someone using one bothers to explain it to me. I've
    # also seen a few installs from AT&T as well, equally baffling.

    60: { 'name': 'Comcast Communications', 'server': '0.0.0.0',
          'filter': '# No rules for Comcast, please suggest some!' },
    61: { 'name': 'AT&T Internet Services', 'server': '0.0.0.0',
          'filter': '# No rules for AT&T, please suggest some!' }
}

# Simplified writer for templates

def expand (handler, name, context):
    path = os.path.join (os.path.dirname (__file__), name)
    handler.response.out.write (template.render (path, context))

# The landing page for human readers to see

class MainHandler (webapp2.RequestHandler):
    def get (self):
        context = {
            'user': users.get_current_user ()
        }
        expand (self, 'index.html', context)

# Since we're sending back data rather than plain text, abstract out the
# wrapping of the data, permitting the caller to ask for JSONP style.

def send (handler, data):
    cb = handler.request.get ('cb', '()')
    if cb == '()':
        handler.response.out.write (data)
        return

    # This header already exists in the collection so adjust rather than add
    handler.response.headers ['Content-Type'] = 'application/json; charset=utf-8'

    if cb != '':
        handler.response.out.write (cb + '(')
    handler.response.out.write (json.dumps (data))
    if cb != '':
        handler.response.out.write (')')

# All the data we care about, all in a dict, for various handlers to choose
# from to render

def bundle (self):
    source = self.request.get ('ip', self.request.remote_addr)
    netblock = find_netblock (source)

    # GAE actually includes a small amount of GeoIP itself; not what need for
    # ISP selection, but interesting nonetheless (note: only in production,
    # not in the dev server)
    # http://code.google.com/appengine/docs/python/runtime.html#Request_Headers

    country = self.request.headers.get ('X-AppEngine-Country')
    country = country or 'Unknown'

    logging.info (source + '(country=' + country + ') mapped to ' + '%d' % netblock)

    isp = isps.get (netblock);
    result = {
        'latest': latest_version,
        'download': code_file_base + latest_file,
        'country': country,
        'ispname': isp ['name'],
        'filterip': isp ['server'],
        'filterrule': isp.get ('filter') or isp ['server'],
        'allow': isp.get ('allow') or ''
    }

    test = isp.get ('test')
    if test:
        result ['test'] = test

    return result

# The query page for the latest revision, which can information about the latest
# version number in various forms

class LatestHandler (webapp2.RequestHandler):
    def get (self):
        send (self, bundle (self) ['latest'])

# A query page for redirecting to the latest download; we can choose between
# a redirect to the Google Code page for the download, or a direct link to the
# download itself.
#
# Attaching ?direct=1 to the GAE /download URL will get us to redirect to the
# file itself, otherwise we show the main download page.

class DownloadHandler (webapp2.RequestHandler):
    def get (self):
        direct = self.request.get ('direct', 'about')

        if direct == 'about':
            to = code_ui_base + 'downloads/detail?name=' + latest_file
        else:
            to = bundle (self) ['download']

        if direct != '0':
            self.redirect (to)
        else:
            send (self, to)

# A query page for exercising the IP->ISP mapping; the bit below for loopback
# is for local testing since that doesn't yield a valid IP for the matching
# algorithm to use.

class IspHandler (webapp2.RequestHandler):
    def get (self):
        send (self, bundle (self) ['ispname'])

# An alternative to the above for getting the IP value to use; here we'll
# always default to the TelstraClear one.
#
# Now that more complex rules are supported, we'll return those or the simple
# ones, as there doesn't need to be back-compat support for returning only the
# simple ones as with the full bundle

class FilterHandler (webapp2.RequestHandler):
    def get (self):
        send (self, bundle (self) ['filterip'])

# Return the newer style of filter list.

class FilterRuleHandler (webapp2.RequestHandler):
    def get (self):
        send (self, bundle (self) ['filterrule'])

# Return a customized server list, or the default global one

class AllowHostHandler (webapp2.RequestHandler):
    def get (self):
        send (self, bundle (self) ['allow'])

# Return a bundle of various of the above individual pieces as a JSON-style
# map.

class BundleHandler (webapp2.RequestHandler):
    def get (self):
        send (self, bundle (self))

# Handle notifying the project owner when a feedback form or uploaded
# submission occurs. For now this is direct, but in future this could
# equally well be done using the cron job API to roll up notifications in
# a batch.
#
# Note that the free tier of GAE severely restricts the amoun of outbound
# e-mail to 100 messages/day, hence why XMPP notification is a better kind
# of default if significant traffic is expected. Not that I do expect any
# more than one or two actual notifications to occur, but it's nice to show

def notifyOwner (text, kind):
    # Send an invitation first so that the GAE instance gets permission
    # as a contact so that later messages are received properly instead of
    # getting binned (as the GTalk servers tend to do, as a spam-control
    # measure).
    #
    # This is particularly useful when running a test instance of a GAE app
    # as each will use a distinct source JID for itself.

    who = 'nigel.bree@gmail.com'
    xmpp.send_invite (who)
    status = xmpp.send_message (who, 'Posted ' + kind + ': ' + text)

    if status == xmpp.NO_ERROR:
        return

    # If XMPP is unavailable, fall back to e-mail

    mail.send_mail ('Feedback <feedback@steam-limiter.appspotmail.com>',
                    'Nigel Bree <nigel.bree@gmail.com>',
                    'New ' + kind + ' posted',
                    text)

# Feedback model for the feedback submission form to persist

class Feedback (db.Model):
    content = db.TextProperty ()
    source = db.StringProperty ()
    timestamp = db.DateTimeProperty (auto_now = True)

# Handle a feedback form, to allow people to spam me with whatever they like...
# given that currently I'm suffering from a lack of feedback, this is meant
# to help overcome that. We shall see if it works.

class FeedbackHandler (webapp2.RequestHandler):
    def get (self):
        expand (self, 'feedback.html', { })

    def post (self):
        text = self.request.get ('content')

        if text != '':
            item = Feedback (content = text, source = self.request.remote_addr)
            item.put ()

            notifyOwner (text, 'feedback')

        expand (self, 'thanks.html', { })

# Similar to the general text feedback, we can have users upload their custom
# rules as suggestions for future versions or revisions of the rule base now
# that the rulebase exists completely in the webservice.

class UploadedRule (db.Model):
    ispName = db.StringProperty ()
    filterRule = db.StringProperty (multiline = True)
    notes = db.StringProperty (multiline = True)
    source = db.StringProperty ()
    country = db.StringProperty ()
    timestamp = db.DateTimeProperty (auto_now = True)

# Handle a new-rule suggestion form, intended to support a future automatic
# upload of a user's custom rules.

class UploadRuleHandler (webapp2.RequestHandler):
    def get (self):
        expand (self, 'uploadrule.html', { })

    def post (self):
        isp = self.request.get ('ispname')
        rule = self.request.get ('filterrule')
        note = self.request.get ('content')

        country = self.request.headers.get ('X-AppEngine-Country')
        country = country or 'Unknown'

        if rule != '':
            item = UploadedRule (ispName = isp, filterRule = rule, notes = note,
                                 source = self.request.remote_addr,
                                 country = country)
            item.put ()

            notifyOwner (isp + ' ==> ' + rule + '\n' + note, 'rule')

        expand (self, 'thanks.html', { })

# Handle a posted report from a special local test - this is primarily used
# in beta builds to see how some of the client-end rule extensions are being
# processed.

class TestReportHandler (webapp2.RequestHandler):
    def get (self):
        expand (self, 'uploadrule.html', { })

    def post (self):
        test = self.request.get ('test')
        result = self.request.get ('result')

        country = self.request.headers.get ('X-AppEngine-Country')
        country = country or 'Unknown'

        notifyOwner (test + ' ==> ' + result + '\n', 'test')
        expand (self, 'thanks.html', { })

# Custom 404 that suggests filing an issue rather than the default blank.

class NotFoundHandler (webapp2.RequestHandler):
    def get (self):
        self.error (404)
        expand (self, 'default_error.html', { })

# Plumb up the GAE boilerplate with a mapping of URLs to handlers.

app = webapp2.WSGIApplication ([('/', MainHandler),
                                ('/latest', LatestHandler),
                                ('/download', DownloadHandler),
                                ('/ispname', IspHandler),
                                ('/filterip', FilterHandler),
                                ('/filterrule', FilterRuleHandler),
                                ('/allow', AllowHostHandler),
                                ('/all', BundleHandler),
                                ('/feedback', FeedbackHandler),
                                ('/uploadrule', UploadRuleHandler),
                                ('/testreport', TestReportHandler),
                                ('/.*', NotFoundHandler)],
                               debug = True)

def main ():
    application.run ()

if __name__ == '__main__':
    main ()
