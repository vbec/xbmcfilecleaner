# encoding: utf-8

import os, sys, platform, time
import re
import shutil, errno
import xbmc, xbmcaddon
from ctypes import c_wchar_p, c_ulonglong, pointer, windll
from sqlite3 import connect, OperationalError

# Addon info
__title__ = "XBMC File Cleaner"
__author__ = "Andrew Higginson <azhigginson@gmail.com>"
__addonID__ = "script.filecleaner"
__icon__ = "special://home/addons/" + __addonID__ + "/icon.png"
__settings__ = xbmcaddon.Addon(__addonID__)

class Cleaner:

    # Constants to ensure correct SQL queries
    MOVIES = "movie"
    MUSIC_VIDEOS = "musicvideo"
    TVSHOWS = "episode"

    def __init__(self):
        """
        Create a Cleaner object that performs regular cleaning of watched videos.
        """
        self.reload_settings()

        service_sleep = 10
        ticker = 0
        delayed_completed = False

        # TODO should be removed: http://ziade.org/2008/01/08/syssetdefaultencoding-is-evil/
        reload(sys)
        sys.setdefaultencoding("utf-8")

        while not xbmc.abortRequested:
            self.reload_settings()

            scanInterval_ticker = self.scanInterval * 60 / service_sleep
            delayedStart_ticker = self.delayedStart * 60 / service_sleep

            if not self.deletingEnabled:
                continue
            elif  not self.runAsService:
                continue
            else:
                if delayed_completed and ticker >= scanInterval_ticker:
                    self.cleanup()
                    ticker = 0
                elif not delayed_completed and ticker >= delayedStart_ticker:
                    delayed_completed = True
                    self.cleanup()
                    ticker = 0

                time.sleep(service_sleep)
                ticker += 1

        # Abort is requested by XBMC: terminate
        self.debug(__settings__.getLocalizedString(34007))

    def cleanup(self):
        """
        Delete any watched videos from the XBMC video database.
        The videos to be deleted are subject to a number of criteria as can be specified in the addon's settings.
        """
        # TODO combine these functionalities into a single loop
        self.debug(__settings__.getLocalizedString(34004))
        if not self.deleteUponLowDiskSpace or (self.deleteUponLowDiskSpace and self.disk_space_low()):
            # create stub to summarize cleaning results
            summary = "Deleted" if not self.holdingEnabled else "Moved"
            cleaningRequired = False
            if self.deleteMovies:
                movies = self.get_expired(self.MOVIES)
                if movies:
                    count = 0
                    for file, path in movies:
                        if os.path.exists(path):
                            cleaningRequired = True
                            if self.holdingEnabled:
                                self.debug("Moving movie %s from %s to %s" % (os.path.basename(file), path, self.holdingFolder))
                                self.move_file(path, self.holdingFolder)
                            else:
                                self.debug("Deleting movie %s from %s" % (os.path.basename(file), path))
                                self.delete_file(path)
                            count += 1
                    summary += " %d %s(s)" % (count, self.MOVIES)

            if self.deleteTVShows:
                episodes = self.get_expired(self.TVSHOWS)
                if episodes:
                    count = 0
                    for file, path, show, season, idFile in episodes:
                        if os.path.exists(path):
                            cleaningRequired = True
                            if self.holdingEnabled:
                                if self.createSubdirectories:
                                    newpath = os.path.join(self.holdingFolder, show, "Season " + season)
                                    self.create_subdirectories(newpath)
                                else:
                                    newpath = self.holdingFolder
                                self.debug("Moving episode %s from %s to %s" % (os.path.basename(file), os.path.dirname(file), newpath))
                                moveOk = self.move_file(path, newpath)
                                if self.updatePaths and moveOk:
                                    self.update_path_reference(idFile, newpath)
                            else:
                                self.delete_file(path)
                            count += 1
                    summary += " %d %s(s)" % (count, self.TVSHOWS)

            if self.deleteMusicVideos:
                musicvideos = self.get_expired(self.MUSIC_VIDEOS)
                if musicvideos:
                    count = 0
                    for file, path in musicvideos:
                        if os.path.exists(path):
                            cleaningRequired = True
                            if self.holdingEnabled:
                                self.debug("Moving music video %s from %s to %s" % (os.path.basename(file), path, self.holdingFolder))
                                self.move_file(path, self.holdingFolder)
                            else:
                                self.debug("Deleting music video %s from %s" % (os.path.basename(file), path))
                                self.delete_file(path)
                            count += 1
                    summary += " %d %s(s)" % (count, self.MUSIC_VIDEOS)

            # Give a status report if any deletes occurred
            if not (summary.endswith("ed")):
                self.notify(summary)

            # Finally clean the library to account for any deleted videos.
            if self.cleanLibrary and cleaningRequired:
                # Wait 10 seconds for deletions to finish before cleaning.
                time.sleep(10)

                pause = 5
                iterations = 0
                limit = self.scanInterval - pause
                # Check if the library is being updated before cleaning up
                while xbmc.getCondVisibility("Library.IsScanningVideo"):
                    iterations += 1

                    # Make sure we don't mess up the scan interval timing by waiting too long.
                    if iterations * pause >= limit:
                        iterations = 0
                        break

                    self.debug("The video library is currently being updated, waiting %d minutes before cleaning up." % pause)
                    time.sleep(pause * 60)

                xbmc.executebuiltin("XBMC.CleanLibrary(video)")

    def get_expired(self, option):
        """
        Retrieve a list of episodes that have been watched and match any criteria set in the addon's settings.
        
        Keyword arguments:
        option -- the type of videos to remove, can be one of the constants MOVIES, TVSHOWS or MUSIC_VIDEOS
        """
        results = []
        margin = 0.000001

        # First we shall build the query to be executed on the video databases

        query = "SELECT strFilename as File, strPath || strFilename as FullPath"
        if option == "episode":
            query += ", idFile, strTitle as Show, c12 as Season"
        query += " FROM %sview" % option # episodeview, movieview or musicvideoview
        query += " WHERE playCount > 0"

        if self.holdingEnabled:
            query += " AND NOT strPath like '%s%%'" % self.holdingFolder

        if self.enableExpiration:
            query += " AND files.lastPlayed < datetime('now', '-%d days', 'localtime')" % self.expireAfter

        if self.deleteOnlyLowRated and option is not self.MUSIC_VIDEOS:
            column = "c05" if option is self.MOVIES else "c03"
            query += " AND %s BETWEEN %f AND %f" % (column, (margin if self.ignoreNoRating else 0), self.minimumRating - margin)
            if self.minimumRating != 10.000000:
                # somehow 10.000000 is considered to be between 0.000001 and x.999999
                query += " AND %s <> 10.000000" % column

        try:
            # After building the query we can execute it on any video databases we find
            folder = os.listdir(xbmc.translatePath("special://database/"))
            for database in folder:
                if database.startswith("MyVideos") and database.endswith(".db"):
                    con = connect(xbmc.translatePath("special://database/" + database))
                    cur = con.cursor()

                    self.debug("Executing query on %s: %s" % (database, query))
                    cur.execute(query)

                    # Append the results to the list of files to delete.
                    results += cur.fetchall()

            return results
        except OSError, e:
            self.debug("Something went wrong while opening the database folder (errno: %d)" % e.errno)
            raise
        except OperationalError, oe:
            # The video database(s) could not be opened, or the query was invalid
            self.notify(__settings__.getLocalizedString(34002), 15000)
            msg = oe.args[0]
            self.debug("The following error occurred: '%s'" % msg)
        finally:
            cur.close()
            con.close()

    def update_path_reference(self, idFile, newPath):
        """
        Update file reference for a file
        
        Keyword arguments:
        idFile -- the id of the file to update the path reference for
        newPath -- the new location for the file
        """
        try:
            folder = os.listdir(xbmc.translatePath('special://database/'))
            for database in folder:
                # Check for any database of any XMBC version and use it for cleaning
                # (e.g. MyVideos34.db / MyVideos60.db / MyVideos75.db)
                if database.startswith('MyVideos') and database.endswith('.db'):
                    con = connect(xbmc.translatePath('special://database/' + database))
                    cur = con.cursor()

                    # Insert path if it doesn't exist
                    query = "INSERT OR IGNORE INTO"
                    query += " path(strPath)"
                    query += " values('%s/')" % newPath

                    self.debug("Executing query on %s: %s" % (database, query))
                    cur.execute(query)

                    # Look up the id of the new path
                    query = "SELECT idPath"
                    query += " FROM path"
                    query += " WHERE strPath = ('%s/')" % newPath

                    self.debug("Executing " + str(query))
                    cur.execute(query)
                    idPath = cur.fetchone()[0]

                    # Update path reference for the moved file
                    query = "UPDATE OR IGNORE files"
                    query += " SET idPath = %d" % idPath
                    query += " WHERE idFile = %d" % idFile

                    self.debug("Executing query on %s: %s" % (database, query))
                    cur.execute(query)
                    con.commit()
        except OSError, e:
            self.debug("Something went wrong while opening the database folder (errno: %d)" % e.errno)
            raise
        except OperationalError, oe:
            # The video database(s) could not be opened, or the query was invalid
            self.notify(__settings__.getLocalizedString(34002), 15000)
            msg = oe.args[0]
            self.debug(__settings__.getLocalizedString(34008) % msg)
        finally:
            cur.close()
            con.close()

    def reload_settings(self):
        """
        Retrieve new values for all settings, in order to account for any recent changes.
        """
        __settings__ = xbmcaddon.Addon(__addonID__)

        self.runAsService = bool(__settings__.getSetting("run_as_service") == "true")
        self.deletingEnabled = bool(__settings__.getSetting("service_enabled") == "true")
        self.delayedStart = float(__settings__.getSetting("delayed_start"))
        self.scanInterval = float(__settings__.getSetting("scan_interval"))

        self.notificationsEnabled = bool(__settings__.getSetting("show_notifications") == "true")
        self.debuggingEnabled = bool(xbmc.translatePath(__settings__.getSetting("enable_debug")) == "true")

        self.enableExpiration = bool(__settings__.getSetting("enable_expire") == "true")
        self.expireAfter = float(__settings__.getSetting("expire_after"))

        self.deleteOnlyLowRated = bool(__settings__.getSetting("delete_low_rating") == "true")
        self.minimumRating = float(__settings__.getSetting("low_rating_figure"))
        self.ignoreNoRating = bool(__settings__.getSetting("ignore_no_rating") == "true")

        self.deleteUponLowDiskSpace = bool(__settings__.getSetting("delete_on_low_disk") == "true")
        self.diskSpaceThreshold = float(__settings__.getSetting("low_disk_percentage"))
        self.diskSpacePath = xbmc.translatePath(__settings__.getSetting("low_disk_path"))

        self.cleanLibrary = bool(__settings__.getSetting("clean_library") == "true")
        self.deleteMovies = bool(__settings__.getSetting("delete_movies") == "true")
        self.deleteTVShows = bool(__settings__.getSetting("delete_tvshows") == "true")
        self.deleteMusicVideos = bool(__settings__.getSetting("delete_musicvideos") == "true")

        self.holdingEnabled = bool(__settings__.getSetting("enable_holding") == "true")
        self.holdingFolder = xbmc.translatePath(__settings__.getSetting("holding_folder"))
        self.createSubdirectories = bool(xbmc.translatePath(__settings__.getSetting("create_series_season_dirs")) == "true")
        self.updatePaths = bool(xbmc.translatePath(__settings__.getSetting("update_path_reference")) == "true")

    def get_free_disk_space(self, path):
        """
        Determine the percentage of free disk space.
        
        Keyword arguments:
        path -- the path to the drive to check (this can be any path of any length on the desired drive). 
        If the path doesn't exist, this function returns 100, in order to prevent files from being deleted accidentally.
        """
        percentage = 100
        self.debug("path is: " + path)
        if os.path.exists(path) or r"://" in path: #os.path.exists() doesn't work for non-UNC network paths
            if platform.system() == "Windows":
                self.debug("We are checking disk space from a Windows file system")
                self.debug("Stripping " + path + " of all redundant stuff.")

                if r"://" in path:
                    self.debug("We are dealing with network paths:\n" + path)
                    # TODO: Verify this regex captures all possible usernames and passwords.
                    pattern = re.compile("(?P<protocol>smb|nfs)://(?P<user>\w+):(?P<pass>[\w\-]+)@(?P<host>\w+)", re.I)
                    match = pattern.match(path)
                    share = match.groupdict()
                    self.debug("Regex result:\nprotocol: %s\nuser: %s\npass: %s\nhost: %s" % (share['protocol'], share['user'], share['pass'], share['host']))
                    path = path[match.end():]
                    self.debug("Creating UNC paths, so Windows understands the shares, result:\n" + path)
                    path = os.path.normcase(r"\\" + share["host"] + path)
                    self.debug("os.path.normcase result:\n" + path)
                else:
                    self.debug("We are dealing with local paths:\n" + path)

                if not isinstance(path, unicode):
                    path = path.decode('mbcs')

                totalNumberOfBytes = c_ulonglong(0)
                totalNumberOfFreeBytes = c_ulonglong(0)

                # GetDiskFreeSpaceEx explained: http://msdn.microsoft.com/en-us/library/windows/desktop/aa364937(v=vs.85).aspx
                windll.kernel32.GetDiskFreeSpaceExW(c_wchar_p(path), pointer(totalNumberOfBytes), pointer(totalNumberOfFreeBytes), None)

                free = float(totalNumberOfBytes.value)
                capacity = float(totalNumberOfFreeBytes.value)

                try:
                    percentage = float(free / capacity * float(100))
                    self.debug("Hard disk checks returned the following results:\n%s: %f\n%s: %f\n%s: %f" % ("free", free, "capacity", capacity, "percentage", percentage))
                except ZeroDivisionError, e:
                    self.notify(__settings__.getLocalizedString(34011), 15000)
            else:
                self.debug("We are checking disk space from a non-Windows file system")
                self.debug("Stripping " + path + " of all redundant stuff.")
                drive = os.path.normpath(path)
                self.debug("The path now is " + drive)

                try:
                    diskstats = os.statvfs(path)
                    percentage = float(diskstats.f_bfree / diskstats.f_blocks * float(100))
                    self.debug("Hard disk checks returned the following results:\n%s: %f\n%s: %f\n%s: %f" % ("free blocks", diskstats.f_bfree, "total blocks", diskstats.f_blocks, "percentage", percentage))
                except OSError, e:
                    self.notify(__settings__.getLocalizedString(34012) % self.diskSpacePath)
                except ZeroDivisionError, zde:
                    self.notify(__settings__.getLocalizedString(34011), 15000)
        else:
            self.notify(__settings__.getLocalizedString(34013), 15000)

        return percentage

    def disk_space_low(self):
        """
        Check if the disk is running low on free space.
        Returns true if the free space is less than the threshold specified in the addon's settings.
        :rtype : Boolean
        """
        return self.get_free_disk_space(self.diskSpacePath) <= self.diskSpaceThreshold

    def delete_file(self, file):
        """
        Delete a file from the file system.
        """
        if os.path.exists(file):
            try:
                os.remove(file)
                self.debug(__settings__.getLocalizedString(34006) % (os.path.basename(file), os.path.dirname(file)))
            except OSError, e:
                self.debug("Deleting file %s failed with error code %d" % (file, e.errno))
        else:
            self.debug("The file '%s' was already deleted" % file)

    def move_file(self, file, destination):
        """
        Move a file to a new destination. Returns True if the move succeeded, False otherwise.
        
        Keyword arguments:
        file -- the file to be moved
        destination -- the new location of the file
        """
        try:
            if os.path.exists(file) and os.path.exists(destination):
                newfile = os.path.join(destination, os.path.basename(file))
                shutil.move(file, newfile)
                self.debug(__settings__.getLocalizedString(34003) % file)
                return True
            else:
                if not os.path.exists(file):
                    self.notify(__settings__.getLocalizedString(34009) % file, 10000)
                else:
                    self.notify(__settings__.getLocalizedString(34010) % destination, 10000)
                return False
        except OSError, e:
            self.debug("Moving file %s failed with error code %d" % (file, e.errno))
            return False

    def create_subdirectories(self, seasondir):
        """
        Create season as well as series directories in the folder specified.
        
        Keyword arguments:
        seasondir -- the directory in which to create the folder(s)
        """
        seriesdir = os.path.dirname(seasondir)
        self.create_directory(seriesdir)
        self.create_directory(seasondir)

    def create_directory(self, location):
        """
        Creates a directory at the location provided.
        """
        try:
            self.debug("Creating directory at %s" % location)
            os.mkdir(location)
        except OSError, e:
            # Ignore existing directory errors
            if e.errno != errno.EEXIST:
                self.debug("Creating directory at %s failed with error code %d" % (location, e.errno))
                raise
            else:
                self.debug("Directory already exists")
        else:
            self.debug("Successfully created directory")

    def notify(self, message, duration=5000, image=__icon__):
        """
        Display an XBMC notification and log the message.

        Keyword arguments:
        message -- the message to be displayed and logged
        duration -- the duration the notification is displayed in milliseconds (default 5000)
        image -- the path to the image to be displayed on the notification (default "icon.png")
        """
        self.debug(message)
        if self.notificationsEnabled:
            xbmc.executebuiltin("XBMC.Notification(%s, %s, %s, %s)" % (__title__, message, duration, image))

    def debug(self, message):
        """
        logs a debug message
        """
        if self.debuggingEnabled:
            xbmc.log(__title__ + ": " + message)

run = Cleaner()
