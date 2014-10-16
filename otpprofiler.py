#!/usr/bin/python

import psycopg2, psycopg2.extras
import urllib2, time, itertools, json
import subprocess, urllib, random
import simplejson

DATE = '10/13/2014'
# split out base and specific endpoint
#URL_BASE = "http://localhost:8080/otp-rest-servlet/ws/"
#URL_PLAN = URL_BASE + 'plan?'
SHOW_PARAMS = False
SHOW_URL = True

def getServerInfo(host):
    """Get information about the server that is being profiled. Returns a tuple of:
    (sha1, version, cpuName, nCores)
    where sha1 is the hash of the HEAD commit, version is the output of 'git describe' (which 
    includes the last tag and how many commits have been made on top of that tag), 
    cpuName is the model of the cpu as reported by /proc/cpuinfo via the OTP serverinfo API,
    and nCores is the number of (logical) cores reported by that same API, including hyperthreading. 
    """
    url_meta = "http://"+host+"/otp/"

    try :
        print "grabbing metadata from %s"%url_meta
        req = urllib2.Request(url_meta)
        req.add_header('Accept', 'application/json')
        response = urllib2.urlopen(req)
        if response.code != 200 :
            print "Server metadata response was not 200"
            return None
        content = response.read()
        objs = json.loads(content)
        print "Server metadata response: ", objs
        version = objs['serverVersion']['version']
        sha1 = objs['serverVersion']['commit']
        # would also be nice to have build date
        cpuName = objs['cpuName']
        nCores = objs['nCores']
    except urllib2.URLError : 
        # This is a normal condition while waiting for server to come up, trap exception and return None.
        # Any problem with decoding the server response will still blow up the program with an exception.
        print "Error requesting metadata from OTP server. Is it running?"
        return None
    print "sha1 of commit is:", sha1
    print "version of OTP is:", version
    print "processor type is:", cpuName
    print "number of logical cores is:", nCores
    return (sha1, version, cpuName, nCores)
                      
def insert (cursor, table, d, returning=None) :
    """Convenience method to insert all key-value pairs from a Python dictionary into a table
    interpreting the dictionary keys as column names. Can optionally return a column from the
    inserted row. This is useful for getting the automatically generated serial ID of the new row.
    """
    # keys and values are guaranteed to be in the same order by python
    colnames = ','.join(d.keys())
    placeholders = ','.join(['%s' for _ in d.values()])
    sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, colnames, placeholders) 
    if returning != None :
        sql += " RETURNING %s" % (returning)
    cursor.execute(sql, d.values())
    if returning != None :
        result = cursor.fetchone()
        return result[0]
    
    
def sqlarray (list):
    return '{%s}' % (','.join(list))


def summarize (itinerary) :
    routes = []
    trips = []
    waits = []
    n_vehicles = 0
    n_legs = 0
    for leg in itinerary['legs'] :
        n_legs += 1
        if 'route' in leg and len(leg['route']) > 0 :
            routes.append(leg['route'])
            n_vehicles += 1
        #trips.append(leg['trip'])
        #waits.append(leg['wait'])
    ret = { 
        'start_time' : time.asctime(time.gmtime(itinerary['startTime'] / 1000)) + ' GMT',
        'duration' : '%d msec' % int(itinerary['duration']),
        'n_legs' : n_legs,
        'n_vehicles' : n_vehicles,
        'walk_distance' : itinerary['walkDistance'],
        'wait_time_sec' : itinerary['waitingTime'],
        'ride_time_sec' : itinerary['transitTime'],
        'routes' : sqlarray(routes),
        'trips' : sqlarray(trips),
        'waits' : sqlarray(waits) }
    return ret
    

def run(connect_args) :
    run_time_id = int(time.time())

    notes = connect_args.pop('notes')
    retry = connect_args.pop('retry')
    fast  = connect_args.pop('fast')

    host = connect_args.pop('host')

#    info = ("0xABCD", "0.7.13-S", "blah", 8)
    info = getServerInfo(host)
    while retry > 0 and info == None:
        print "Failed to connect to OTP server. Waiting to retry (%d)." % retry
        time.sleep(5)
        info = getServerInfo(host)
        retry -= 1
        
    if info == None :
        print "Failed to identify OTP version. Exiting."
        exit(-2)

    connect_args.pop('password')
    connect_args.pop('port')
    connect_args.pop('user')
    # Connection on local UNIX domain socket without password will by default use peer authentication
    # Peer authentication will be used if host, port, and user are not present (popped out of args dictionary)
    # Replace with sqlAlchemy?
    # Presumably by the time OTP server is up, Postgres should also be up.
    try:
        # create separate connection for reading, to allow use of both a server-side cursor
        # and progressive commits

        print connect_args

        read_conn = psycopg2.connect(**connect_args)
        read_cur = read_conn.cursor('read_cur', cursor_factory=psycopg2.extras.DictCursor)
        write_conn = psycopg2.connect(**connect_args)
        write_cur = write_conn.cursor()
        print "Connections established to database server."
    except:
        print "Unable to connect to the database. Exiting."
        exit(-1)

    import getpass
    user_name = getpass.getuser()
    run_row = info + (notes,user_name)
    write_cur.execute("INSERT INTO runs (run_began, run_ended, automated, git_sha1, git_describe, "
                "cpu_name, cpu_cores, notes, user_name)"
                "VALUES (now(), NULL, TRUE, %s, %s, %s, %s, %s, %s) RETURNING run_id", run_row)
    write_conn.commit() # commit to make sure now() is evaluated before run starts                
    run_id = write_cur.fetchone()[0]
    print "run id", run_id

    run_json = dict(zip(('git_sha1','git_describe','cpu_name','cpu_cores','notes','user_name'),run_row))
    run_json['id'] = run_time_id

    # note double quotes in SQL string to force case-sensitivity on query param columns.
    # origin/destination matrix is constrained to be lower-triangular since we do both 
    # depart-after and arrive-by searches. this halves the number of searches.
    # the set of all combinations is filtered such that only the 'typical' requests (i.e. tuples of
    # query parameters) are combined with the (more numerous) random endpoints, but all reqests are 
    # combined with the (presumably less numerous) explicitly defined endpoints.
    # that is, in every combination retained, the request is either considered typical, or in the
    # case that the request is atypical, the endpoints are not random. 
    # only like pairs of endpoints are considered (random to random, nonrandom to nonrandom).
    if fast :
        PARAMS_SQL = """ SELECT requests.*,
        origins.endpoint_id AS oid, origins.lat || ',' || origins.lon AS "fromPlace",
        targets.endpoint_id AS tid, targets.lat || ',' || targets.lon AS "toPlace"
        FROM requests, endpoints AS origins, endpoints AS targets
        WHERE origins.random IS FALSE AND targets.random IS TRUE AND 
          (requests.typical IS TRUE); """
    else :
        PARAMS_SQL = """ SELECT requests.*,
        origins.endpoint_id AS oid, origins.lat || ',' || origins.lon AS "fromPlace",
        targets.endpoint_id AS tid, targets.lat || ',' || targets.lon AS "toPlace"
        FROM requests, endpoints AS origins, endpoints AS targets
        WHERE origins.endpoint_id < targets.endpoint_id AND 
          origins.random = targets.random AND 
          (requests.typical IS TRUE OR origins.random IS FALSE); """

    read_cur.execute(PARAMS_SQL)
    all_params = read_cur.fetchall()
    random.shuffle(all_params)
    n = 0
    N = len(all_params)
    t0 = time.time()
    response_json = []
    full_itins_json = []
    for params in all_params : # fetchall takes time and mem, use a server-side named cursor
        n += 1
        t = (time.time() - t0) / 60.0
        T = (N * t) / n
        print "Request %d/%d, time %0.2f min of %0.2f (estimated) " % (n, N, t, T)
        params = dict(params) # could also use a RealDictCursor
        request_id = params.pop('request_id')
        oid = params.pop('oid')
        tid = params.pop('tid')
        # not necessary if OD properly constrained in SQL 
        #if oid == tid :
        #    continue
        params['date'] = DATE
        # Tomcat server + spaces in URLs -> HTTP 505 confusion
        url = "http://"+host+"/otp/routers/default/plan?" + urllib.urlencode(params)
        if SHOW_PARAMS :
            print params
        if SHOW_URL :
            print url
        req = urllib2.Request(url)
        req.add_header('Accept', 'application/json')
        start_time = time.time()
        response = urllib2.urlopen(req)
        end_time = time.time()
        elapsed = end_time - start_time
        n_itin = 0
        if response.code != 200 :
            print "not 200"
            status = 'failed'
        else :
            content = response.read()
            objs = json.loads(content)

            if 'plan' in objs:
                itineraries = objs['plan']['itineraries']
                n_itin = len(itineraries)
                print n_itin, 'itineraries'
                # check response for timeout flag
                status = 'complete'
                path_times = objs['debugOutput']['pathTimes']
                print path_times
                # status = 'timed out'
            else:
                print 'no itineraries'
                status = 'no paths'
                
        row = { 'run_id' : run_id,
                'request_id' : request_id,
                'origin_id' : oid,
                'target_id' : tid,
                'total_time' : str(elapsed) + ' seconds',
                'avg_time' : None if n_itin == 0 else '%f seconds' % (float(elapsed) / n_itin),
                'status' : status,
                'membytes' : None }
        response_id = insert (write_cur, 'responses', row, returning='response_id') 
        row['response_id'] = response_id
        row['itins'] = []
        response_json.append( row )
        
        # Create a row for each itinerary within this single trip planner result
        if (n_itin > 0) :
            for (itinerary_number, itinerary) in enumerate(itineraries) :
                itin_row = summarize (itinerary)
                itin_row['response_id'] = response_id
                itin_row['itinerary_number'] = itinerary_number + 1
                
                full_itin = {'response_id':response_id,'itinerary_number':itinerary_number+1}
                full_itin['body']=itinerary
                full_itins_json.append( full_itin )

                insert (write_cur, 'itineraries', itin_row) # no return (automatic serial key) value needed
                row['itins'].append( itin_row )
        write_conn.commit()
    
    write_cur.execute( "UPDATE runs SET run_ended=now() WHERE run_id=%s", (run_id,) )
    write_conn.commit()

    fpout = open("run_summary.%s.json"%run_time_id,"w")
    run_json['responses'] = response_json
    simplejson.dump(run_json, fpout, indent=2)
    fpout.close()

    fpout = open("full_itins.%s.json"%run_time_id,"w")
    simplejson.dump( full_itins_json, fpout, indent=2 )
    fpout.close()
    
import argparse
if __name__=="__main__":
    import argparse # optparse is deprecated
    parser = argparse.ArgumentParser(description='perform an otp profiler run') 
    parser.add_argument('host') 
    parser.add_argument('-f', '--fast', action='store_true', default=False) 
    parser.add_argument('-p', '--password')
    parser.add_argument('-u', '--user', default='ubuntu') 
    parser.add_argument('-P', '--port', type=int, default='8000') 
    parser.add_argument('-d', '--database', default='otpprofiler') 
    parser.add_argument('-n', '--notes') 
    parser.add_argument('-r', '--retry', type=int, default=3) 
    args = parser.parse_args() 

    # args is a non-iterable, non-mapping Namespace (allowing usage as args.name), so convert it to a dict
    run(vars(args))


