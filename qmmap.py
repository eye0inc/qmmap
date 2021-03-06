#
# mongo Operations
#
import sys, os, importlib, datetime, time, traceback, __main__
import socket

import bson
import pymongo
from pymongo.read_preferences import ReadPreference
from multiprocessing import Process
import mongoengine as meng
from mongoengine.context_managers import switch_collection

NULL = open(os.devnull, "w")
BATCH_SIZE = 600  # Set input batch size; mongo will limit it if it's too much

def is_shell():
    return sys.argv[0] == "" or sys.argv[0][-8:] == "/ipython"

class housekeep(meng.Document):
    start = meng.DynamicField(primary_key = True)
    end = meng.DynamicField()
    total = meng.IntField()  # total # of entries to do
    good = meng.IntField(default = 0)  # entries successfully processed
#     bad = meng.IntField(default = 0)                    # entries we failed to process to completion
#     log = meng.ListField()                              # log of misery -- each item a failed processing incident
    state = meng.StringField(default = 'open')
    # Globally unique identifier for the process, if any, that is working on
    # this chunk, to know if something else is working on it
    procname = meng.StringField(default = 'none')
#     git = meng.StringField()                                 # git commit of this version of source_destination
    tstart = meng.DateTimeField()  # Time when job started
    time = meng.DateTimeField()  # Time when job finished
    meta = {'indexes': ['state', 'time']}

def _connect(srccol, destcol, dest_uri=None):
    connectMongoEngine(destcol, dest_uri)
    hk_colname = srccol.name + '_' + destcol.name
    switch_collection(housekeep, hk_colname).__enter__()

def _init(srccol, destcol, key, query, chunk_size, verbose):
    housekeep.drop_collection()
    q = srccol.find(query, [key]).sort([(key, pymongo.ASCENDING)])
    if verbose & 2: print "initializing %d entries, housekeeping for %s" % (q.count(), housekeep._get_collection_name())
#     else:
#         raise Exception("no incremental yet")
# #         last = housekeep.objects().order_by('-start')[0].end
# #         if verbose & 2: print "last partition field in housekeep:", last
# #         query[key + "__gt"] = last
# #         q = srccol.objects(**query).only(key).order_by(key)
# #         if verbose & 2: print "added %d entries to %s" % (q.count(), housekeep._get_collection_name())
# #         sys.stdout.flush()
    i = 0
    tot = q.limit(chunk_size).count(with_limit_and_skip=True)
    while tot > 0:
        if verbose & 2: print "housekeeping: %d" % i
        i +=1
        sys.stdout.flush()
        hk = housekeep()
        hk.start = q[0][key]
        hk.end =  q[min(chunk_size-1, tot-1)][key]
        if (hk.start == None or hk.end == None):
            if verbose & 2: print >> sys.stderr, "ERROR: key field has None. start: %s end: %s" % (hk.start, hk.end)
            raise Exception("key error")
        #calc total for this segment
        qq = {'$and': [query, {key: {'$gte': hk.start}}, {key: {'$lte': hk.end}}]}
        hk.total = srccol.find(qq, [key]).count()
        hk.save()

        #get start of next segment
        qq = {'$and': [query, {key: {'$gt': hk.end}}]}
        q = srccol.find(qq, [key]).sort([(key, pymongo.ASCENDING)])
        #limit count to chunk for speed
        tot = q.limit(chunk_size).count(with_limit_and_skip=True)


def _is_okay_to_work_on(hkstart):
    """Returns whether a chunk, identified by its housekeeping start value, is okay
to work on, i.e. whether its status is "working" and this process is assigned to it
    """
    if not hkstart:  # Can ignore if specific chunk not specified
        return True
    chunk = housekeep.objects.get(start=hkstart)
    # If it's been reset to open, or being worked on by another node, no good
    state = chunk.state
    if state == "done":
        print "Chunk {0} is already finished".format(hkstart)
        sys.stdout.flush()
        return False
    if state == "open":
        print "Chunk {0} had been reset to open".format(hkstart)
        sys.stdout.flush()
        return False
    if state == "working" and chunk.procname != procname():
        print "Chunk {0} was taken over by {1}, moving on".format(
            hkstart, chunk.procname)
        sys.stdout.flush()
        return False
    return True


def _doc_size(doc):
    """Returns the size, in bytes of a Mongo object
    @doc: Mongo document in native Mongo format
    """
    return len(bson.BSON.encode(doc))


def _copy_cursor(cursor):
    """Returns a new cursor with the same properites that won't affect the original
    @cursor: any cursor that hasn't already been iterated over

    @return: new cursor with same attributes
    """
    new_cursor = cursor.collection.find()
    new_cursor.__dict__.update(cursor.__dict__)
    return new_cursor


def _write_bulk(bulk):
    """Execute bulk write `bulk` and note the errors
    """
    try:
        bulk.execute()
    except:
        _print_proc("***BULK WRITE EXCEPTION (process)***")
        _print_proc(traceback.format_exc())
        _print_proc("***END EXCEPTION***")


def _process(init, proc, src, dest, verbose, hkstart=None):
    """Run process `proc` on cursor `src`.
    @hkstart: primary key of houskeeping chunk that this is processing, if you are
using one and which to avoid collisions
    """
    if not verbose & 1:
        oldstdout = sys.stdout
        sys.stdout = NULL
    global context
    if init:
        try:
            # Pass a copy of the source and destination cursors so they won't
            # affect iteration in the rest of _process
            context = init(_copy_cursor(src), _copy_cursor(dest))
        except:
            _print_proc("***EXCEPTION (process)***")
            _print_proc(traceback.format_exc())
            _print_proc("***END EXCEPTION***")
            return 0
    good = 0
    # After you've accumulated this many bytes of objects, execute the bulk
    # write and start it over
    WRITE_THRESHOLD = 10000000
    inserts = 0
    # Before starting, check if some other process has taken over; in that
    # case, exit early with -1
    if not _is_okay_to_work_on(hkstart):
        return -1
    bulk = dest.initialize_unordered_bulk_op()
    src.batch_size(BATCH_SIZE)
    insert_size = 0  # Size, in bytes, of all objects to be written
    insert_count = 0 # Number of inserts
    for doc in src:
        try:
            ret = proc(doc)
            if ret != None:
                # If doing housekeeping, save for bulk insert since that will know
                # whether these would be duplicate inserts
                if hkstart:
                    # if _id in ret, search by that and upsert/update_one;
                    # assume that all non-_id, non-$ keys need to be updated with
                    # the $set operator
                    if '_id' in ret:
                        bulk.find({'_id': ret['_id']}).upsert().update_one(
                            {'$set': ret}
                        )
                    else:
                        # if no _id, do simple insert
                        bulk.insert(ret)
                    insert_size += _doc_size(ret)
                    insert_count += 1
                    # If past the threshold, do another check and write
                    if insert_size > WRITE_THRESHOLD:
                        if not _is_okay_to_work_on(hkstart):
                            return -1
                        print u"Writing to chunk {0} : {1} docs totaling " \
                            u"{2} bytes".format(hkstart, insert_count, insert_size)
                        sys.stdout.flush()
                        _write_bulk(bulk)
                        bulk = dest.initialize_unordered_bulk_op()
                        insert_size = 0
                        insert_count = 0
                else:
                    # No housekeeping checks, so save immediately with DB check
                    dest.save(ret)
                inserts += 1
            good += 1
        except:
            _print_proc("***EXCEPTION (process)***")
            _print_proc(traceback.format_exc())
            _print_proc("***END EXCEPTION***")
    # After processing, check again if okay to insert
    sys.stdout.flush()
    if not _is_okay_to_work_on(hkstart):
        return -1
    if hkstart:  # Do bulk insert only if doing housekeeping
        if insert_count > 0:
            _print_proc(u"Writing to chunk {0} : {1} docs totaling {2} " \
                u"bytes".format(hkstart, insert_count, insert_size))
            _write_bulk(bulk)
        else:
            _print_proc(u"No bulk writes to do at end of chunk" \
                u" {0}".format(hkstart))
    if not verbose & 1:
        sys.stdout = oldstdout
    sys.stdout.flush()
    return good


context = {}


def do_chunks(init, proc, src_col, dest_col, query, key, sort, verbose, sleep=60):
    while housekeep.objects(state = 'done').count() < housekeep.objects.count():
        tnow = datetime.datetime.utcnow()
        raw = housekeep._collection.find_and_modify(
            {'state': 'open'},
            {
                '$set': {
                    'state': 'working',
                    'tstart': tnow,
                    'procname': procname(),
                }
            }
        )
        # if raw==None, someone scooped us
        if raw != None:
            raw_id = raw['_id']
            #reload as mongoengine object -- _id is .start (because set as primary_key)
            hko = housekeep.objects(start = raw_id)[0]
            # Record git commit for sanity
#             hko.git = git.Git('.').rev_parse('HEAD')
#             hko.save()
            # get data pointed to by housekeep
            qq = {'$and': [query, {key: {'$gte': hko.start}}, {key: {'$lte': hko.end}}]}
            # Make cursor not timeout, using version-appropriate paramater
            if pymongo.version_tuple[0] == 2:
                cursor = src_col.find(qq, timeout=False)
            elif pymongo.version_tuple[0] == 3:
                cursor = src_col.find(qq, no_cursor_timeout=True)
            else:
                raise Exception("Unknown pymongo version")
            # Set the sort parameters on the cursor
            if sort[0] == "-":
                cursor = cursor.sort(sort[1:], pymongo.DESCENDING)
            else:
                cursor = cursor.sort(sort, pymongo.ASCENDING)
            if verbose & 2: print "mongo_process: %d elements in chunk %s-%s" % (cursor.count(), hko.start, hko.end)
            sys.stdout.flush()
            # This is where processing happens
            hko.good =_process(init, proc, cursor, dest_col, verbose,
                hkstart=raw_id)
            # Check if another job finished it while this one was plugging away
            hko_later = housekeep.objects(start = raw_id).only('state')[0]
            if hko.good == -1:  # Early exit signal
                print "Chunk at %s lost to another process; not updating" % raw_id
                sys.stdout.flush()
            elif hko_later.state == 'done':
                print "Chunk at %s had already finished; not updating" % raw_id
                sys.stdout.flush()
            else:
                hko.state = 'done'
                hko.procname = 'none'
                hko.time = datetime.datetime.utcnow()
                hko.save()
        else:
            # Not all done, but none were open for processing; thus, wait to
            # see if one re-opens
            print 'Standing by for reopening of "working" job...'
            sys.stdout.flush()
            time.sleep(sleep)


def _num_not_at_state(state):
    """Helper for consisely counting the number of housekeeping objects at a
given state
    """
    return housekeep.objects(state__ne=state).count()


# balance chunk size vs async efficiency etc
# otherwise try for at least 10 chunks per proc
#
def _calc_chunksize(count, multi, chunk_size=None):
    if chunk_size != None:
        return chunk_size
    cs = count/(multi*10.0)
    cs = max(cs, 10)
    if count / float(cs * multi) < 1.0:
        cs *= count / float(cs * multi)
        cs = max(1, int(cs))
    return int(cs)



def procname():
    """Utility for getting a globally-unique process name, which needs to combine
hostname and process id
@returns: string with format "<fully qualified hostname>:<process id>"."""
    return "{:>18}:{}".format(socket.getfqdn(), os.getpid())


def mmap(   cb,
            source_col,
            dest_col,
            init=None, 
            reset=False,
            source_uri="mongodb://127.0.0.1/test", 
            dest_uri="mongodb://127.0.0.1/test",
            query={},
            key='_id',
            sort='_id',
            verbose=1,
            multi=None,
            wait_done=True,
            init_only=False,
            process_only=False,
            manage_only=False,
            chunk_size=None,
            timeout=120,
            sleep=60,
            **kwargs):

    # Two different connect=False idioms; need to set it false to wait on
    # connecting in case of process being spawned.
    if pymongo.version_tuple[0] == 2:
        dbs = pymongo.MongoClient(
            source_uri, read_preference=ReadPreference.SECONDARY_PREFERRED,
            _connect=False,
        ).get_default_database()
        dbd = pymongo.MongoClient(dest_uri, _connect=False).get_default_database()
    else:
        dbs = pymongo.MongoClient(
            source_uri, read_preference=ReadPreference.SECONDARY_PREFERRED,
            connect=False,
        ).get_default_database()
        dbd = pymongo.MongoClient(dest_uri, connect=False).get_default_database()
    dest = dbd[dest_col]
    if multi == None:  # don't use housekeeping, run straight process

        source = dbs[source_col].find(query)
        _process(init, cb, source, dest, verbose)
    else:
        _connect(dbs[source_col], dest, dest_uri)
        if manage_only:
            manage(timeout, sleep)
        elif not process_only:
            computed_chunk_size = _calc_chunksize(
                dbs[source_col].find(query).count(), multi, chunk_size)
            if verbose & 2: print "chunk size:", computed_chunk_size
            if reset:
                print >> sys.stderr, ("Dropping all records in destination db" +
                    "/collection {0}/{1}").format(dbd, dest.name)
                dest.remove({})
            _init(dbs[source_col], dest, key, query, computed_chunk_size, verbose)
        # Now process code, if one of the other "only_" options isn't turned on
        if not manage_only and not init_only:
            args = (init, cb, dbs[source_col], dest, query, key, sort, verbose,
                sleep)
            if verbose & 2:
                print "Chunking with arguments %s" % (args,)
            if is_shell():
                print >> sys.stderr, ("WARNING -- can't generate module name. Multiprocessing will be emulated...")
                do_chunks(*args)
            else:
                if multi > 1:
                    for j in xrange(multi):
                        if verbose & 2:
                            print "Launching subprocess %s" % j
                        proc = Process(target=do_chunks, args=args)
                        proc.start()
                else:
                    do_chunks(*args)
            if wait_done:
                manage(timeout, sleep)
                #wait(timeout, verbose & 2)
    return dbd[dest_col]

def toMongoEngine(pmobj, metype):
    meobj = metype._from_son(pmobj)
    meobj.validate()
    return meobj


def qmmapify(meng_class):
    """Decorator for turning a `process` function writeen for mongoengine objects,
to a process function written for pymongo objects (and therefore compatible with
QMmap.
    params:
    @meng_class: mongoengine class for the type that the mongoengine function
    expects as an argument
    """
    def pymongo_process_fn(meng_process_fn):
        def wrapper(pymongo_source):
            input_meng_obj = toMongoEngine(pymongo_source, meng_class)
            output_meng_obj = meng_process_fn(input_meng_obj)
            # If it returned an object at all, convert that to pymongo
            if output_meng_obj:
                return output_meng_obj.to_mongo()
            else:
                return None
        return wrapper
    return pymongo_process_fn


def connectMongoEngine(pmcol, conn_uri=None):
    if pymongo.version_tuple[0] == 2:     #really? REALLY?
        #host = pmcol.database.connection.HOST
        #port = pmcol.database.connection.PORT
        host = pmcol.database.connection.host
        port = pmcol.database.connection.port
    else:
        host = pmcol.database.client.HOST
        port = pmcol.database.client.PORT
    # Can just use the connection uri, which has credentials
    if conn_uri:
        return meng.connect(pmcol.database.name, host=conn_uri)
    return meng.connect(pmcol.database.name, host=host, port=port)

def remaining():
    return housekeep.objects(state__ne = "done").count()

def wait(timeout=120, verbose=True):
    t = time.time()
    r = remaining()
    rr = r
    while r:
#         print "DEBUG r %f rr %f t %f" % (r, rr, time.time() - t)
        if time.time() - t > timeout:
            if verbose: print >> sys.stderr, "TIMEOUT reached - resetting working chunks to open"
            q = housekeep.objects(state = "working")
            if q:
                q.update(state="open", procname='none')
        if r != rr:
            t = time.time()
        if verbose: print r, "chunks remaning to be processed; %f seconds left until timeout" % (timeout - (time.time() - t)) 
        time.sleep(1)
        rr = r
        r = remaining()


def _print_proc(log_str):
    """Utility function for writing to STDERR with procname prepended
    @log_str: string to write
    """
#     print >> sys.stderr, procname(), log_str
#make atomic so no interrupted output lines:
    sys.stderr.write("%s %s\n" % (procname(), log_str) )
    sys.stderr.flush()


def _print_progress():
    q = housekeep.objects(state = 'done').only('time')
    tot = housekeep.objects.count()
    done = q.count()
    if done > 0:
        pdone = 100. * done / tot
        q = q.order_by('time')
        first = q[0].time
        q = q.order_by('-time')
        last = q[0].time
        if first and last:  # guard against lacking values
            tdone = float((last-first).seconds)
            ttot = tdone*tot / done
            trem = ttot - tdone
            print "%s still waiting: %d out of %d complete (%.3f%%). %.3f seconds complete, %.3f remaining (%.5f hours)" \
            % (datetime.datetime.utcnow().strftime("%H:%M:%S:%f"), done, tot, pdone, tdone, trem, trem / 3600.0)
        else:
            print "No progress data yet"
    else:
        print "%s still waiting; nothing done so far" % (datetime.datetime.utcnow(),)
sys.stdout.flush()


def manage(timeout, sleep=120):
    """Give periodic status, reopen dead jobs, return success when over;
    combination of wait, status, clean, and the reprocessing functions.
    sleep = time (sec) between status updates
    timeout = time (sec) to give a job until it's restarted
    """
    num_not_done = _num_not_at_state('done')
    print "Managing job's execution; currently {0} remaining".format(num_not_done)
    sys.stdout.flush()
    # Keep going until none are state=working or done
    while _num_not_at_state('done') > 0:
        # Sleep before management step
        time.sleep(sleep)
        _print_progress()
        # Now, iterate over state=working jobs, restart ones that have gone
        # on longer than the timeout param
        tnow = datetime.datetime.utcnow()  # get time once instead of repeating
        # Iterate through working objects to see if it's too long
        hkwquery = [h for h in housekeep.objects(state='working').all()]
        for hkw in hkwquery:
            # .tstart must have time value for state to equal 'working' at all
            time_taken = (tnow - hkw.tstart).total_seconds()
            print (u"Chunk on {0} starting at {1} has been working for {2} " +
                u"sec").format(hkw.procname, hkw.start, time_taken)
            sys.stdout.flush()
            if time_taken > timeout:
                print (u"More than {0} sec spent on chunk {1} ;" +
                    u" setting status back to open").format(
                    timeout, hkw.start)
                sys.stdout.flush()
                hkw.state = "open"
                hkw.procname = 'none'
                hkw.save()
    print "----------- PROCESSING COMPLETED ------------"
