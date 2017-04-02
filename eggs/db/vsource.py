#!/usr/bin/python
#
# Copyright (c) 2006 - 2017, Hewlett-Packard Development Co., L.P. 
# Description: SQLite virtual tables for Vertica data collectors
# Author: DingQiang Liu

import atexit
from multiprocessing.dummy import Pool as ThreadPool
from functools import partial
import threading
from datetime import datetime
import time
from dateutil import parser as datetimeparser
from decimal import Decimal
import struct
from operator import ior
from itertools import islice

import db.vcluster as vcluster
import db.vdatacollectors as vdatacollectors

def getLastSQLiteActivityTime() :
  global __g_LastSQLiteActivityTime
  try:
    if not __g_LastSQLiteActivityTime is None :
      return __g_LastSQLiteActivityTime
  except NameError:
    __g_LastSQLiteActivityTime = time.time()
  return __g_LastSQLiteActivityTime

def setLastSQLiteActivityTime(newtime) :
  global __g_LastSQLiteActivityTime
  __g_LastSQLiteActivityTime = newtime


def setup(connection):
  """ Register datacollectors of Vertica
  Arguments:
    connection: apsw.Connection
  """

  VerticaSource(connection);


def splitListGroups(alist, n):
  """ split list to n groups """
  d, m = divmod(len(alist), n)
  if d == 0 :
    n = m
  it = iter(alist)
  for i in range(n):
    yield list(islice(it, d+(i<m)))


# module for vertica sources
class VerticaSource:
  def __init__(self, connection):
    self.tables = {}
    self.ddls = {} # Note: ddl must end with ");" , syncJob will add primary key before it.
    self.connection = connection
    connection.createmodule("verticasource", self)
  
    # create datacollector virtual tables
    vdatacollectors.create(self)

    # start data sync job if main database not in memory
    self.syncJobCursor = None
    if connection.filename != "" :
      # start data sync job
      self.syncJobCursor = connection.cursor()
      self.stopSyncJobEvent = threading.Event()
      t = threading.Thread(target=self.syncJob)
      t.daemon = True
      t.start()
      # stop data sync job automatically when exiting
      atexit.register(self.stopSyncJob)

    connection.setexectrace(self.exectracer)


  def stopSyncJob(self) :
    self.stopSyncJobEvent.set()

  
  def syncJob(self) :
    cursor = self.syncJobCursor
    while not self.stopSyncJobEvent.is_set() :
      for tablename in self.tables :
        # active sync job after at least 5 seconds of last sqlite activity
        #time.sleep(1)
        while time.time() - getLastSQLiteActivityTime() < 30 :
          time.sleep(10);

        try :
          tbegin = time.time()

          # create real SQLite table. Actually we copy DDL of origional table and add PRIMARY KEY/WITHOUT ROWID for efficent storage and performance
          tables = [ t for (t) in cursor.execute("select tbl_name from sqlite_master where lower(tbl_name)  = ?", (tablename, )) ]
          if len(tables) == 0 :
            cursor.execute("drop table if exists %s" % tablename+"_tmp")
            
            # set primary key for datacollectors
            cursor.execute(self.ddls[tablename].replace(tablename, tablename+"_tmp").replace(");",",PRIMARY KEY(%s)) WITHOUT ROWID;" % ",".join(self.tables[tablename].primaryKeys)))
            
            cursor.execute("insert into %s select * from %s" % (tablename+"_tmp", tablename))
            cursor.execute("alter table %s rename to %s" % (tablename+"_tmp", tablename))
          else :
            # filter on time on virtual table into temp table, for better performance
            cursor.execute("drop table if exists __tmpdc")
            cursor.execute("create temp table __tmpdc as select * from v_internal.%s where time > (select min(time) from (select max(time) time from main.%s group by node_name))" % (tablename, tablename))
            cursor.execute("insert into main.%s select * from __tmpdc where (%s) not in (select %s from main.%s)" % (tablename, ",".join(self.tables[tablename].primaryKeys), ",".join(self.tables[tablename].primaryKeys), tablename))
            cursor.execute("drop table if exists __tmpdc")
            # TODO: rotate tablesize
            #cursor.execute("delete from main.%s where time < oldest-permit-for-size" % tablename)
          
          #print "DEBUG: [syncJob] sync data of table [%s] in %.1f seconds." % (tablename, time.time() - tbegin)
        except Exception, e:
          msg = str(e)
          if not "InterruptError:" in msg :
            print "ERROR: sync data for table [%s] from Vertica because [%s]" % (tablename, msg)


  def Create(self, db, modulename, dbname, tablename, *args):
    table = Table(tablename)
    self.tables[tablename] = table
    return self.ddls[tablename], table

  Connect=Create
  
  def exectracer(self, cursor, sql, bindings):
    if not cursor is self.syncJobCursor :
      # ignore background sync job
	    # tell background sync job it's busy now.
      setLastSQLiteActivityTime(time.time())
      #print "DEBUG: [EXECTRACER] CURSOR=%s, SQL=%s, BINDINGS=%s" % (cursor, sql, bindings)

    return True



# table for datacollector
class Table:
  def __init__(self, tablename):
    self.tablename=tablename
    # Note: self.remotefiltermodule will be set in *.create function just after "create virtual table TABLENAME using verticasource"
    self.columns = []
    self.columnTypes = {}

  def BestIndex(self, constraints, orderbys):
    """
    filter on time and node_name. Vertica datacollector log files can be looked as "order by node_name, time segmented by node_name all nodes"
      Node: 
      1. execution order: BestIndex+ Open Filter+ Eof+ Column*
      2. APSW does not support WITHOUT ROWID virtual table at now. SQLite will try all possible index. eg, 
          for "time >= ? and node_name = ? or time >= ? and node_name = ?", the logica may be filter virtual table two times: 
          [row for rowid in ((rowid filter on index_time and index_nodename) union (rowid filter on index_time and index_nodename)))]
    """

    #print "DEBUG: [BESTINDEX] tablename=%s, constraints=%s, orderbys=%s" % (self.tablename, constraints, orderbys)
    if len(constraints) > 0 and any([ 1 if columnIndex in (0,1,) else 0 for (columnIndex, predicate) in constraints ]) == 1 : 
      # only filter on time(0) and node_name(1) column
      # arg appearance order
      argOrders = []
      i = 0
      for (columnIndex, predicate) in constraints : 
        if columnIndex in (0, 1) :
          argOrders.append(i)
          i += 1
        else :
          argOrders.append(None)
      # indexID: 1: time, 2: node_name, 3: time and nodename
      indexID = reduce(ior, [ columnIndex+1 if columnIndex in (0,1,) else 0 for (columnIndex, predicate) in constraints ])
      # indexName: columnIndx_predicate[+columnIndx_predicate]*
      indexName = "+".join([ "%s_%s" % (columnIndex, predicate) for (columnIndex, predicate) in filter(lambda x: x[0] in (0,1,), constraints) ])
      # cost
      cost = {1: 10, 2: 1000, 3: 1}[indexID] 

      return (argOrders, indexID, indexName, False, cost)
    else : 
      return None

  def Open(self):
    cursor = Cursor(self)
    #print "    DEBUG: [Open] CURSOR=%s" % cursor
    return cursor

  def Disconnect(self):
    pass

  Destroy=Disconnect

    

# cursor for datacollector
class Cursor:
  def __init__(self, table):
    self.table = table
    self.data = None
    self.pos=0


  def Filter(self, indexnum, indexname, constraintargs):
    # get data 
    self.data = []
    self.pos=0
    vc = vcluster.getVerticaCluster()
    #predicates {columnIndx: [[predicate1:value1, predicate2:value2]]} 
    predicates = {}
    columns = self.table.columns
    if not indexname is None and len(indexname) > 0 and not constraintargs is None and len(constraintargs) > 0 :
      #indexname columnIndx_predicate_[+columnIndx_predicate]* 
      for i, pred in enumerate(indexname.split("+")) :
        lstPred = pred.split("_")
        col = int(lstPred[0])
        op = int(lstPred[1])
        val = constraintargs[i]
        if col == 0 :
          # time: string to long
          if not val is None :
            val = long(datetimeparser.parse(val).strftime('%s%f'))-946684800*1000000
        predCol = predicates[col] if col in predicates else []
        predCol.append([op, val])
        predicates[col] = predCol

    #print "    DEBUG: [FILTER] tablename=%s, cursor=%s, pos=%s, indexnum=%s, indexname=%s, constraintargs=%s, predicates=%s" % (self.table.tablename, self, self.pos, indexnum, indexname, constraintargs, predicates)
    # call remote function
    mch = vc.executors.remote_exec(self.table.remotefiltermodule)
    mch.send_each({"catalogpath":vc.catPath, "tablename":self.table.tablename, "columns":columns, "predicates":predicates})

    q = mch.make_receive_queue(endmarker=None)
    terminated = 0
    while 1:
      channel, rows = q.get()
      if rows is None :
        terminated += 1
        if terminated == len(mch):
          break
        continue
      else: 
        # TODO: why multiple threads parsing not benifit for performance? Where is the bottleneck?
        self.data.extend(self.parseRows(rows, columns))
        #self.data.extend(self.parseRowsParallel(rows, columns))
        #self.data.extend(self.parseRowsParallel2(rows, columns))
    
  def parseRowsParallel2(self, rows, columns):
    # multiple thread parsing for better performance
    pool = ThreadPool()
    rowsGroups = pool.map( partial(self.parseRows2, columns=columns) , [i for i in splitListGroups(rows.split('\2'), pool._processes)] )
    pool.close()
    pool.join()
    
    dataRows = []
    for g in rowsGroups :
      dataRows.extend(g) 
    return dataRows

  def parseRows2(self, rows, columns):
    dataRows = []
    for line in rows:
      try :
        # ignore broken line
        colValues = line.split('\1')
        if len(colValues) == len(columns) :
          dataRows.append( [ parseValue(self.table.columnTypes[columns[i]], cv) for i, cv in enumerate(colValues) ] )
      except Exception, e:
        raise StandardError("[%s] when parseRows [%s] of column [%s] on table [%s]" % (str(e), cv, self.table.columns[i], self.table.tablename))
    return dataRows

  def parseRowsParallel(self, rows, columns):
    # multiple thread parsing for better performance
    pool = ThreadPool()
    dataRows = pool.map( partial(self.parseLine, columns=columns) , [ line for line in rows.split('\2') ] )
    pool.close()
    pool.join()
    return [x for x in dataRows if x is not None] # ignore broken line 

  def parseRows(self, rows, columns):
    dataRows = []
    for line in rows.split('\2'):
      try :
        # ignore broken line
        colValues = line.split('\1')
        if len(colValues) == len(columns) :
          dataRows.append( [ parseValue(self.table.columnTypes[columns[i]], cv) for i, cv in enumerate(colValues) ] )
      except Exception, e:
        raise StandardError("[%s] when parseRows [%s] of column [%s] on table [%s]" % (str(e), cv, self.table.columns[i], self.table.tablename))
    return dataRows

  def parseLine(self, line, columns):
    try :
      colValues = line.split('\1')
      if len(colValues) == len(columns) :
        return [ parseValue(self.table.columnTypes[columns[i]], cv) for i, cv in enumerate(colValues) ]
      else :
        return None
    except Exception, e:
      raise StandardError("[%s] when parseLine [%s] of column [%s] on table [%s]" % (str(e), cv, self.table.columns[i], self.table.tablename))

  def Eof(self):
    return self.pos>=len(self.data)

  def Rowid(self):
    return self.data[self.pos][0]

  def Column(self, col):
    #if (col == 0) : print "    DEBUG: [COLUMN] tablename=%s, cursor=%s, pos=%s" % (self.table.tablename, self, self.pos)

    try :
      return self.data[self.pos][1+col]
    except Exception, e:
      raise StandardError("[%s] when get value [%s] of column [%s] on table [%s]" % (str(e), self.data[self.pos][1+col], self.table.columns[1+col], self.table.tablename))

  def Next(self):
    self.pos+=1

  def Close(self):
    self.data = None


def parseValue(sqltype, value):
  # Till now, Vertica datacollector tables only use types: BOOLEAN, FLOAT, INTEGER, TIMESTAMP WITH TIME ZONE, VARCHAR
  if (len(value) == 0) and not sqltype in ('varchar', 'char') :
    return None
  
  try :
    if sqltype in ('integer', 'int', 'bigint', 'smallint', 'mediumint', 'tinyint', 'int2', 'int8') :
      # convert unsigned long to negative long. Note: INTEGER is numeric(18,0) in Vertica, eg. '18442240474082184385' means -4503599627367231
      lValue = long(value)
      if lValue <= 0x7fffffffffffffff :
        return lValue
      else :
        return struct.unpack('l', struct.pack('L', lValue))[0] 
    elif sqltype in ('double', 'float', 'real') :
      return float(value)
    elif sqltype in ('date', 'datetime', 'timestamp') :
      lValue = long(value)
      # -9223372036854775808(-0x8000000000000000) means null in Vertica
      if lValue == -0x8000000000000000 :
          return None
      # 946684800 is secondes between '1970-01-01 00:00:00'(Python) and '2000-01-01 00:00:00'(Vertica)
      return datetime.fromtimestamp(float(lValue)/1000000+946684800).strftime("%Y-%m-%d %H:%M:%S.%f")
    elif sqltype == 'boolean' :
        return 'true' == value.lower()
    elif sqltype in ('decimal', 'numeric', 'boolean') :
      return Decimal(value)
    elif sqltype == 'blob' :
      return buffer(value)
    else :
      # others are str. 
      # process escpe character in string, eg. '\n'
      return value.decode('string_escape')
  except :
    # ignore incorrect value format
    return None