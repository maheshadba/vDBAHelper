#!/usr/bin/python
#
# Copyright (c) 2006 - 2017, Hewlett-Packard Development Co., L.P. 
# Description: SQLite virtual tables for Vertica data collectors
# Author: DingQiang Liu

import unittest
from optparse import OptionParser

import db.vcluster as vcluster
import db.vdatacollectors as vdatacollectors
from testdb.testDataCollectors import *

if __name__ == "__main__":
  parser = OptionParser()  
  parser.add_option("-d", "--database", dest="vDBName", default="") 
  parser.add_option("-f", "--file", dest="vMetaFile", default="/opt/vertica/config/admintools.conf") 
  parser.add_option("-u", "--user", dest="vAdminOSUser", default="dbadmin") 
  (options, args) = parser.parse_args()
  vcluster.getVerticaCluster(vDbName = options.vDBName, vMetaFile = options.vMetaFile, vAdminOSUser = options.vAdminOSUser)

  sqliteDBFile = ""
  if len(args) > 0 :
    sqliteDBFile = args[0]
  CONNECTION = apsw.Connection(sqliteDBFile)
  vdatacollectors.setup(CONNECTION)


  suiteDataCollector = unittest.makeSuite(TestDataCollectors, 'test')
  
  alltests = unittest.TestSuite((suiteDataCollector))
  
  runner = unittest.TextTestRunner(verbosity=2)
  runner.run(alltests)
