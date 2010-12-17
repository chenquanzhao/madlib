# XXXX TODO: copy Config.yml to scripts when synthesizing script!
# XXXX TODO: sanity() method to compare scripts and DB.
# XXXX TODO: remove explicit use of "madlib" schema from method scripts
# XXXX TODO: multiple migrations for single version?

# MADPack Migrations are modeled on Django South and Rails Migrations.
# It allows rolling forward/backward across multiple versions of MADlib.

# Like those tools, we use a directory of migration scripts that provide
# code for rolling forward and backward across versions of the DB.
# But because MADlib is made up of many methods, we synthesize those scripts
# from individual scripts stored registered in the Install.yml for each
# method/port.  The MADlib Config.yml file specifies the relevant method/ports
# to install.

# We use two separate numbering schemes:
#  - the Migration number is a sequence number for 
#    roll-forward/rollback history on a particular DB/schema.  These 
#    numbers are auto-generated by the MadPackMigration class and
#    basically for internal use.  They are generated as dense (consecutive)
#    integers, but the important feature is that they have an order that can
#    be rolled forward/backward, which is a key of the migrationhistory table
#    in the database.
#
#  - the Version number is a MADlib release version, copied from the 
#    Version.yml file.  We assume that users will typically think in terms
#    of version numbers, though Migration numbers are exposed in case there's
#    need to fiddle with them.
#
# Migration information is stored in two places, following the design of 
# South and Rails:
#  - migration scriptfiles named <MigrationNumber>_<Version>.py are stored 
#    in a designated "scripts" directory.  Default is 
#    <python-site-packages>/madpy/config/scripts, but different script 
#    directories can be used (e.g. to support different MADlib versions in 
#    different databases or schemas).
#  - in the relevant database schema (default "madlib"), there is a table  
#    called migrationhistory that keeps track of the names of the scripts
#    installed (in order), and the dates they were installed.
# There may be migrations in the scripts directory that have not been 
# installed (or that were uninstalled).


import os
import shutil
import sqlparse
import sys
import glob
import imp
import traceback
import hashlib
import madpy

# A Python exception class for our use
class MadPackMigrationError(Exception):
    pass
    
class MadPackMigration:
    def __init__(self, mig_dir, conf_dir):
        self.conf = madpy.madpack.configyml.get_config(conf_dir)        
        connect_args = self.conf['connect_args']
        api = self.conf['dbapi2']
        dbapi2 = __import__(api, globals(), locals(), [])
        self.mig_dir = mig_dir
        self.conf_dir = conf_dir

        # migration files have names of the form
        #    <mig_number>_<version_name>.py        
        # where mig_number is mig_number_len digits long
        self.mig_number_len = 3
        
        # the junk that goes at the top of each migration file
        self.mig_prolog = """#!/usr/bin/python
import sys
from madpy.madpack.migrations import MadPackMigration

class Migration(MadPackMigration):
"""

        # the junk that goes before each method's forwards script
        self.mig_forwards_prolog = """\tdef forwards(self):\n\t\tcur = self.dbconn.cursor()\n"""

        # the junk that goes before each method's backwards script
        self.mig_backwards_prolog = """\tdef backwards(self):\n\t\tcur = self.dbconn.cursor()\n"""
        
        # Connect to DB
        self.dbconn = dbapi2.connect(*connect_args)

    def __del__(self):
        self.dbconn.close()
        
    # pad string "strng" on the left with "n" copies of character "prefixchar"
    def __pad_on_left_to_n(self,strng,prefixchar,n):
        return "".join([prefixchar for i in range(0,n - len(strng))]) + strng

    # method to load migration code that we will generate.
    # from http://code.davidjanes.com/blog/2008/11/27/how-to-dynamically-load-python-code/
    def __load_module(self, code_path):
        try:
            try:
                code_dir = os.path.dirname(code_path)
                code_file = os.path.basename(code_path)

                fin = open(code_path, 'rb')
                h = hashlib.md5()
                h.update(code_path)
                return  imp.load_source(h.hexdigest(), code_path, fin)
            finally:
                try: fin.close()
                except: pass
        except ImportError, x:
            traceback.print_exc(file = sys.stderr)
            raise
        except:
            traceback.print_exc(file = sys.stderr)
            raise
    
    # scan migration script names, and map versions to migration numbers
    def __map_versions_to_nums(self):
        vnmap = dict()
        migfiles = glob.glob(self.mig_dir+"/"+"".join(["[0-9]" for i in range (0,self.mig_number_len)])+"_*.py")
        for f in migfiles:
            ver = f.split("_")[1].split(".py")[0]
            mig = f.split("_")[0].split("/")[-1]
            vnmap[ver]=mig
        return vnmap
    
    # map a specific version v to its migration number 
    def __map_version_to_num(self, v, vnmap=None):
        if vnmap == None:
            vnmap = self.__map_versions_to_nums()
        return int(vnmap[v])
        
    # find highest-numbered migration file. 
    def max_file(self):
        numlist = [int(i.split('_')[0]) for i in os.listdir(self.mig_dir) if i.split('_')[0].isdigit()]
        if len(numlist) > 0:
            return max(numlist)
        else:
            return 0

    # find current migration in database
    def __current_mig(self):
        cur = self.dbconn.cursor()
        try:
            cur.execute("SELECT migration FROM "
                        + self.conf['target_schema'] + 
                        ".migrationhistory ORDER BY id DESC LIMIT 1;")
        except:
            print sys.exc_info()[0]
            print "Unexpected error creating " \
                  + self.conf['target_schema'] + ".migrationhistory in database:"
            raise
        row = cur.fetchone()
        if row == None:
            return None
        filename = row[0]
        return filename
        
    def current_mig_number(self):
        curmig = self.__current_mig()
        if curmig == None:
            return -1
        else:
            return int(curmig.split("_")[0])

    def current_version(self):
        curmig = self.__current_mig()
        if curmig == None:
            return None
        else:
            return self.__current_mig().split("_")[1].split(".py")[0]

    # list migration files (lo to hi) whose number is > what's in the DB
    def fw_files(self):
        files = [i for i in os.listdir(self.mig_dir) if i.split('_')[0].isdigit() and int(i.split('_')[0]) > self.current_mig_number() and i.split(".")[-1] == "py"]
        files.sort()
        return files

    # list migration files (hi to lo) whose number is <= what's in the DB
    def bw_files(self):
        files = [i for i in os.listdir(self.mig_dir) if i.split('_')[0].isdigit() and int(i.split('_')[0]) <= self.current_mig_number() and i.split(".")[-1] == "py"]
        files.sort(reverse=True)
        return files

    # generate a new migration filename, with number one larger than max
    def __gen_filename(self, name):
        next_num = self.current_mig_number()+1
        name = self.__pad_on_left_to_n(str(next_num),'0',self.mig_number_len)+"_"+name
        if name.split('.')[-1] != "py":
            name += ".py"
        return name

    # do a shallow parse of an SQL file, and wrap each stmt with 
    # Python dbapi2 call syntax.
    def __wrap_sql(self,sqlfile,indent_width):
        fd = open(sqlfile)
        sqltext = "".join(fd.readlines())
        stmts = sqlparse.split(sqltext)
        retval = ""
        for s in stmts:
            if s.strip() != "":
                retval += "".join(["\t" for i in range(0,indent_width)]) + "cur.execute(\"\"\"" + s.strip() + "\"\"\")"
                retval += "\n"
        return retval

    # given the upfiles (fw) and downfiles (bw) for a set of methods,
    #  generate a new migration file and place in dir
    def generate(self, dir, name, upfiles, downfiles):
        self.setup()
        filename = self.__gen_filename(name)
        # while os.path.exists(dir + filename)
        fd = open(dir + "/" + filename, 'w')
        fd.write(self.mig_prolog)
        fd.write(self.mig_forwards_prolog)
        for f in upfiles:
            fd.write(self.__wrap_sql(f,2))
        fd.write("\n\n\n")
        fd.write(self.mig_backwards_prolog)
        for f in downfiles:
            fd.write(self.__wrap_sql(f,2))
        return filename
        
    # create migrations directory and metadata in DB.
    # schema is:
    #    id:          serial
    #    migration:   varchar(255)
    #    applied:     timestamp with time zone
    def setup(self):
        cur = self.dbconn.cursor()
        try:
            cur.execute("""SELECT table_schema, table_name
                             FROM information_schema.tables
                            WHERE table_schema = %s
                              AND table_name = 'migrationhistory';""",
                        (self.conf['target_schema'],))
        except:
            raise MadPackMigrationError("Unexpected error checking " 
                                        + self.conf['target_schema'] + 
                                        + " schema in database")
            
        if cur.fetchone() == None:
            # check for schema
            cur.close()
            cur = self.dbconn.cursor()
            try:
                cur.execute("""SELECT * FROM information_schema.schemata
                                WHERE schema_name = %s;""", 
                            (self.conf['target_schema'],))
            except:
                print sys.exc_info()[0]
                print "Unexpected error checking "+self.conf['target_schema']+" schema in database:"
                raise
            if cur.fetchone() == None:
                print sys.exc_info()[0]
                raise MadPackMigrationError("%s schema not defined in database", (self.conf['target_schema'],))
            cur.close()
            cur = self.dbconn.cursor()
            try:
                cur.execute("CREATE TABLE " + self.conf['target_schema'] + 
                            ".migrationhistory (id serial, \
                             migration varchar(255),\
                             applied timestamp with time zone)")
            except:
                print sys.exc_info()[0]
                print "Unexpected error creating madlib.migrationhistory in database:"
                raise
            cur.close()
            self.dbconn.commit()

    # record the application of a forward migration in migrationhistory table
    def record_migration(self, filename):
        cur = self.dbconn.cursor()
        try:
            cur.execute("INSERT INTO " + self.conf['target_schema'] + 
                        ".migrationhistory (migration, applied) VALUES (%s, now());", (filename,))
        except:
            print sys.exc_info()[0]
            raise #MadPackMigrationError("Unexpected error recording into madlib.migrationhistory in database:")
        return True
        
    def delete_migration(self, filename):
        cur = self.dbconn.cursor()
        try:
            cur.execute("DELETE FROM " + self.conf['target_schema'] +               
                        ".migrationhistory WHERE migration = %s;",
                        (filename,))
        except:
            print sys.exc_info()[0]
            print "Unexpected error deleting from " + \
                  self.conf['target_schema']+".migrationhistory in database:"
            raise
        return True
        
    # roll migrations fw/bw from current to desired.
    # desire can be expressed by migration number or by version 
    # (but not both.)
    # there has to be a pre-existing migration script that matches.
    def migrate(self, mignumber=None, migversion=None):
       connect_args=self.conf['connect_args'], 
       api = self.conf['dbapi2']
       if mignumber and migversion:
           raise MadPackMigrationError("more than one of {num,version} passed to migrate")
       if migversion:
           mignumber = self.__map_version_to_num(migversion)
       cur = self.current_mig_number()
       maxfile = self.max_file()
       if maxfile == None:
           return
       if mignumber == None:
           mignumber = maxfile
       if mignumber > maxfile:
           print sys.exc_info()[0]
           raise MadPackMigrationError("migration number " + str(mignumber) + " is larger than max file number " + str(maxfile))
       if cur == None or mignumber > cur:
           files = [f for f in self.fw_files() if int(f.split("_")[0]) <= mignumber]
           if len(files) > 0:
               print "- migrating forwards to " + files[-1]
           # rolling fw
           for f in files:
               num = int(f.split("_")[0])
               print "> " + f
               mod = self.__load_module(self.mig_dir+"/"+f)
               m = mod.Migration(self.mig_dir, self.conf_dir)
               m.forwards()
               m.record_migration(f)
               m.dbconn.commit()
       elif mignumber < cur:
           # rolling bw
           files = [f for f in self.bw_files() if int(f.split("_")[0]) > mignumber]
           if len(files) > 0:
               print "- migrating backwards to before " + files[-1]
           for f in files:
               num = int(f.split("_")[0])
               print "> " + f
               mod = self.__load_module(self.mig_dir+"/"+f)
               m = mod.Migration(self.mig_dir, self.conf_dir)
               m.backwards()
               m.delete_migration(f)
               m.dbconn.commit()