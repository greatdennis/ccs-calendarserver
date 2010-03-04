##
# Copyright (c) 2010 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##
from twisted.internet.defer import succeed, inlineCallbacks, DeferredList,\
    returnValue
from twext.web2 import responsecode
from twext.web2.http import HTTPError, Response
from twext.web2.dav.http import ErrorResponse, MultiStatusResponse
from twext.web2.dav.util import allDataFromStream
from twext.web2.dav.element.base import PCDATAElement
from twistedcaldav.sql import AbstractSQLDatabase, db_prefix
from twext.python.log import LoggingMixIn
import os
from twistedcaldav.config import config
from uuid import uuid4

__all__ = [
    "SharingMixin",
]

from twistedcaldav import customxml
from twext.web2.dav import davxml

"""
Sharing behavior
"""

class SharingMixin(object):
    
    def invitesDB(self):
        
        if not hasattr(self, "_invitesDB"):
            self._invitesDB = InvitesDatabase(self)
        return self._invitesDB

    def inviteProperty(self, request):
        
        # Build the CS:invite property from our DB
        def sharedOK(isShared):
            if config.Sharing.Enabled and isShared:
                self.validateInvites()
                return customxml.Invite(
                    *[record.makePropertyElement() for record in self.invitesDB().allRecords()]
                )
            else:
                return None
        return self.isShared(request).addCallback(sharedOK)

    def upgradeToShare(self, request):
        """ Upgrade this collection to a shared state """
        
        # Change resourcetype
        rtype = self.resourceType()
        rtype = davxml.ResourceType(*(rtype.children + (customxml.SharedOwner(),)))
        self.writeDeadProperty(rtype)
        
        # Create invites database
        self.invitesDB().create()

        return succeed(True)
    
    def downgradeFromShare(self, request):
        
        # Change resource type
        rtype = self.resourceType()
        rtype = davxml.ResourceType(*([child for child in rtype.children if child != customxml.SharedOwner()]))
        self.writeDeadProperty(rtype)
        
        # Remove all invitees

        # Remove invites database
        self.invitesDB().remove()
        delattr(self, "_invitesDB")
    
        return succeed(True)

    def removeUserFromInvite(self, userid, request):
        """ Remove a user from this shared calendar """
        self.invitesDB().removeRecordForUserID(userid)            

        return succeed(True)

    def isShared(self, request):
        """ Return True if this is an owner shared calendar collection """
        return succeed(self.isSpecialCollection(customxml.SharedOwner))

    def isVirtualShare(self, request):
        """ Return True if this is a shared calendar collection """
        return succeed(self.isSpecialCollection(customxml.Shared))

    def validUserIDForShare(self, userid):
        """
        Test the user id to see if it is a valid identifier for sharing and return a "normalized"
        form for our own use (e.g. convert mailto: to urn:uuid).

        @param userid: the userid to test
        @type userid: C{str}
        
        @return: C{str} of normalized userid or C{None} if
            userid is not allowed.
        """
        
        # First try to resolve as a principal
        principal = self.principalForCalendarUserAddress(userid)
        if principal:
            return principal.principalURL()
        
        # TODO: we do not support external users right now so this is being hard-coded
        # off in spite of the config option.
        #elif config.Sharing.AllowExternalUsers:
        #    return userid
        else:
            return None

    def validateInvites(self):
        """
        Make sure each userid in an invite is valid - if not re-write status.
        """
        
        records = self.invitesDB().allRecords()
        for record in records:
            if self.validUserIDForShare(record.userid) is None and record.state != "INVALID":
                record.state = "INVALID"
                self.invitesDB().addOrUpdateRecord(record)
                
    def removeVirtualShare(self, request):
        """ As user of a shared calendar, unlink this calendar collection """
        return succeed(False) 

    def getInviteUsers(self, request):
        return succeed(True)

    def sendNotificationOnChange(self, icalendarComponent, request, state="added"):
        """ Possibly send a push and or email notification on a change to a resource in a shared collection """
        return succeed(True)

    def inviteUserToShare(self, userid, ace, summary, request, commonName="", shareName="", add=True):
        """ Send out in invite first, and then add this user to the share list
            @param userid: 
            @param ace: Must be one of customxml.ReadWriteAccess or customxml.ReadAccess
        """
        
        # Check for valid userid first
        userid = self.validUserIDForShare(userid)
        if userid is None:
            return succeed(False)

        # TODO: Check if this collection is shared, and error out if it isn't
        hosturl = self.fp.path
        if type(userid) is not list:
            userid = [userid]
        if type(commonName) is not list:
            commonName = [commonName]
        if type(shareName) is not list:
            shareName = [shareName]
            
        dl = [self.inviteSingleUserToShare(user, ace, summary, hosturl, request, cn=cn, sn=sn) for user, cn, sn in zip(userid, commonName, shareName)]
        return DeferredList(dl).addCallback(lambda _:True)

    def uninviteUserToShare(self, userid, ace, request):
        """ Send out in uninvite first, and then remove this user from the share list."""
        
        # Do not validate the userid - we want to allow invalid users to be removed because they
        # may have been valid when added, but no longer valid now. Clients should be able to clear out
        # anything known to be invalid.

        # TODO: Check if this collection is shared, and error out if it isn't
        if type(userid) is not list:
            userid = [userid]
        return DeferredList([self.uninviteSingleUserFromShare(user, ace, request) for user in userid]).addCallback(lambda _:True)

    def inviteUserUpdateToShare(self, userid, aceOLD, aceNEW, summary, request, commonName="", shareName=""):

        # Check for valid userid first
        userid = self.validUserIDForShare(userid)
        if userid is None:
            return succeed(False)

        hosturl = self.fp.path
        if type(userid) is not list:
            userid = [userid]
        if type(commonName) is not list:
            commonName = [commonName]
        if type(shareName) is not list:
            shareName = [shareName]
        dl = [self.inviteSingleUserUpdateToShare(user, aceOLD, aceNEW, summary, hosturl, request, commonName=cn, shareName=sn) for user, cn, sn in zip(userid, commonName, shareName)]
        return DeferredList(dl).addCallback(lambda _:True)

    def inviteSingleUserToShare(self, userid, ace, summary, hosturl, request, cn="", sn=""):
        
        # Send invite
        inviteuid = str(uuid4())
        
        # Add to database
        self.invitesDB().addOrUpdateRecord(Invite(inviteuid, userid, inviteAccessMapFromXML[type(ace)], "NEEDS-ACTION", summary))
        
        return succeed(True)            

    def uninviteSingleUserFromShare(self, userid, aces, request):
        
        # Cancel invites

        # Remove from database
        self.invitesDB().removeRecordForUserID(userid)
        
        return succeed(True)            

    def inviteSingleUserUpdateToShare(self, userid, acesOLD, aceNEW, summary, hosturl, request, commonName="", shareName=""):
        
        # Just update existing
        return self.inviteSingleUserToShare(userid, aceNEW, summary, hosturl, request, commonName, shareName) 

    def xmlPOSTNoAuth(self, encoding, request):
        def _handleErrorResponse(error):
            if isinstance(error.value, HTTPError) and hasattr(error.value, "response"):
                return error.value.response
            return Response(code=responsecode.BAD_REQUEST)

        def _handleInvite(invitedoc):
            def _handleInviteSet(inviteset):
                userid = None
                access = None
                summary = None
                for item in inviteset.children:
                    if isinstance(item, davxml.HRef):
                        for attendeeItem in item.children:
                            if isinstance(attendeeItem, PCDATAElement):
                                userid = attendeeItem.data
                        continue
                    if isinstance(item, customxml.InviteSummary):
                        for summaryItem in item.children:
                            if isinstance(summaryItem, PCDATAElement):
                                summary = summaryItem.data
                        continue
                    if isinstance(item, customxml.ReadAccess) or isinstance(item, customxml.ReadWriteAccess):
                        access = item
                        continue
                if userid and access and summary:
                    return (userid, access, summary)
                else:
                    if userid is None:
                        raise HTTPError(ErrorResponse(
                            responsecode.FORBIDDEN,
                            (customxml.calendarserver_namespace, "valid-request-content-type"),
                            "missing href: %s" % (inviteset,),
                        ))
                    if access is None:
                        raise HTTPError(ErrorResponse(
                            responsecode.FORBIDDEN,
                            (customxml.calendarserver_namespace, "valid-request-content-type"),
                            "missing access: %s" % (inviteset,),
                        ))
                    if summary is None:
                        raise HTTPError(ErrorResponse(
                            responsecode.FORBIDDEN,
                            (customxml.calendarserver_namespace, "valid-request-content-type"),
                            "missing summary: %s" % (inviteset,),
                        ))

            def _handleInviteRemove(inviteremove):
                userid = None
                access = []
                for item in inviteremove.children:
                    if isinstance(item, davxml.HRef):
                        for attendeeItem in item.children:
                            if isinstance(attendeeItem, PCDATAElement):
                                userid = attendeeItem.data
                        continue
                    if isinstance(item, customxml.ReadAccess) or isinstance(item, customxml.ReadWriteAccess):
                        access.append(item)
                        continue
                if userid is None:
                    raise HTTPError(ErrorResponse(
                        responsecode.FORBIDDEN,
                        (customxml.calendarserver_namespace, "valid-request-content-type"),
                        "missing href: %s" % (inviteremove,),
                    ))
                if len(access) == 0:
                    access = None
                else:
                    access = set(access)
                return (userid, access)

            def _autoShare(isShared, request):
                if not isShared:
                    return self.upgradeToShare(request)
                else:
                    return succeed(True)

            @inlineCallbacks
            def _processInviteDoc(_, request):
                setDict, removeDict, updateinviteDict = {}, {}, {}
                for item in invitedoc.children:
                    if isinstance(item, customxml.InviteSet):
                        userid, access, summary = _handleInviteSet(item)
                        setDict[userid] = (access, summary)
                    elif isinstance(item, customxml.InviteRemove):
                        userid, access = _handleInviteRemove(item)
                        removeDict[userid] = access

                # Special case removing and adding the same user and treat that as an add
                okusers = set()
                badusers = set()
                sameUseridInRemoveAndSet = [u for u in removeDict.keys() if u in setDict]
                for u in sameUseridInRemoveAndSet:
                    removeACL = removeDict[u]
                    newACL, summary = setDict[u]
                    updateinviteDict[u] = (removeACL, newACL, summary)
                    del removeDict[u]
                    del setDict[u]
                for userid, access in removeDict.iteritems():
                    result = (yield self.uninviteUserToShare(userid, access, request))
                    (okusers if result else badusers).add(userid)
                for userid, (access, summary) in setDict.iteritems():
                    result = (yield self.inviteUserToShare(userid, access, summary, request))
                    (okusers if result else badusers).add(userid)
                for userid, (removeACL, newACL, summary) in updateinviteDict.iteritems():
                    result = (yield self.inviteUserUpdateToShare(userid, removeACL, newACL, summary, request))
                    (okusers if result else badusers).add(userid)

                # Do a final validation of the entire set of invites
                self.validateInvites()
                
                # Create the multistatus response - only needed if some are bad
                if badusers:
                    xml_responses = []
                    xml_responses.extend([
                        davxml.StatusResponse(davxml.HRef(userid), davxml.Status.fromResponseCode(responsecode.OK))
                        for userid in sorted(okusers)
                    ])
                    xml_responses.extend([
                        davxml.StatusResponse(davxml.HRef(userid), davxml.Status.fromResponseCode(responsecode.FORBIDDEN))
                        for userid in sorted(badusers)
                    ])
                
                    #
                    # Return response
                    #
                    returnValue(MultiStatusResponse(xml_responses))
                else:
                    returnValue(responsecode.OK)
                    

            return self.isShared(request).addCallback(_autoShare, request).addCallback(_processInviteDoc, request)

        def _getData(data):
            try:
                doc = davxml.WebDAVDocument.fromString(data)
            except ValueError, e:
                self.log_error("Error parsing doc (%s) Doc:\n %s" % (str(e), data,))
                raise HTTPError(ErrorResponse(responsecode.FORBIDDEN, (customxml.calendarserver_namespace, "valid-request-content")))

            root = doc.root_element
            xmlDocHanders = {
                customxml.InviteShare: _handleInvite, 
            }
            if type(root) in xmlDocHanders:
                return xmlDocHanders[type(root)](root).addErrback(_handleErrorResponse)
            else:
                self.log_error("Unsupported XML (%s)" % (root,))
                raise HTTPError(ErrorResponse(responsecode.FORBIDDEN, (customxml.calendarserver_namespace, "valid-request-content")))

        return allDataFromStream(request.stream).addCallback(_getData)

    def xmlPOSTPreconditions(self, _, request):
        if request.headers.hasHeader("Content-Type"):
            mimetype = request.headers.getHeader("Content-Type")
            if mimetype.mediaType in ("application", "text",) and mimetype.mediaSubtype == "xml":
                encoding = mimetype.params["charset"] if "charset" in mimetype.params else "utf8"
                return succeed(encoding)
        raise HTTPError(ErrorResponse(responsecode.FORBIDDEN, (customxml.calendarserver_namespace, "valid-request-content-type")))

    def xmlPOSTAuth(self, request):
        d = self.authorize(request, (davxml.Read(), davxml.Write()))
        d.addCallback(self.xmlPOSTPreconditions, request)
        d.addCallback(self.xmlPOSTNoAuth, request)
        return d
    
    def http_POST(self, request):
        if self.isCollection():
            contentType = request.headers.getHeader("content-type")
            if contentType:
                contentType = (contentType.mediaType, contentType.mediaSubtype)
                if contentType in self._postHandlers:
                    return self._postHandlers[contentType](self, request)
                else:
                    self.log_info("Get a POST of an unsupported content type on a collection type: %s" % (contentType,))
            else:
                self.log_info("Get a POST with no content type on a collection")
        return responsecode.FORBIDDEN

    _postHandlers = {
        ("application", "xml") : xmlPOSTAuth,
        ("text", "xml") : xmlPOSTAuth,
    }

inviteAccessMapToXML = {
    "read-only"  : customxml.ReadAccess,
    "read-write" : customxml.ReadWriteAccess,
}
inviteAccessMapFromXML = dict([(v,k) for k,v in inviteAccessMapToXML.iteritems()])

inviteStatusMapToXML = {
    "NEEDS-ACTION" : customxml.InviteStatusNoResponse,
    "ACCEPTED"     : customxml.InviteStatusAccepted,
    "DECLINED"     : customxml.InviteStatusDeclined,
    "DELETED"      : customxml.InviteStatusDeleted,
    "INVALID"      : customxml.InviteStatusInvalid,
}
inviteStatusMapFromXML = dict([(v,k) for k,v in inviteStatusMapToXML.iteritems()])

class Invite(object):
    
    def __init__(self, inviteuid, userid, access, state, summary):
        self.inviteuid = inviteuid
        self.userid = userid
        self.access = access
        self.state = state
        self.summary = summary
        
    def makePropertyElement(self):
        
        return customxml.InviteUser(
            customxml.UID.fromString(self.inviteuid),
            davxml.HRef.fromString(self.userid),
            customxml.InviteAccess(inviteAccessMapToXML[self.access]()),
            inviteStatusMapToXML[self.state](),
        )

class InvitesDatabase(AbstractSQLDatabase, LoggingMixIn):
    
    db_basename = db_prefix + "invites"
    schema_version = "1"
    db_type = "invites"

    def __init__(self, resource):
        """
        @param resource: the L{twistedcaldav.static.CalDAVFile} resource for
            the shared collection. C{resource} must be a calendar/addressbook collection.)
        """
        self.resource = resource
        db_filename = os.path.join(self.resource.fp.path, InvitesDatabase.db_basename)
        super(InvitesDatabase, self).__init__(db_filename, True)

    def create(self):
        """
        Create the index and initialize it.
        """
        self._db()

    def allRecords(self):
        
        records = self._db_execute("select * from INVITE order by USERID")
        return [self._makeRecord(row) for row in (records if records is not None else ())]
    
    def recordForUserID(self, userid):
        
        row = self._db_value_for_sql("select * from INVITE where USERID = :1", userid)
        return self._makeRecord(row) if row else None
    
    def recordForInviteUID(self, inviteUID):

        row = self._db_value_for_sql("select * from INVITE where INVITEUID = :1", inviteUID)
        return self._makeRecord(row) if row else None
    
    def addOrUpdateRecord(self, record):

        self._db_execute("""insert or replace into INVITE (USERID, INVITEUID, ACCESS, STATE, SUMMARY)
            values (:1, :2, :3, :4, :5)
            """, record.userid, record.inviteuid, record.access, record.state, record.summary,
        )
    
    def removeRecordForUserID(self, userid):

        self._db_execute("delete from INVITE where USERID = :1", userid)
    
    def removeRecordForInviteUID(self, inviteUID):

        self._db_execute("delete from INVITE where INVITEUID = :1", inviteUID)
    
    def remove(self):
        
        self._db_close()
        os.remove(self.dbpath)

    def _db_version(self):
        """
        @return: the schema version assigned to this index.
        """
        return InvitesDatabase.schema_version

    def _db_type(self):
        """
        @return: the collection type assigned to this index.
        """
        return InvitesDatabase.db_type

    def _db_init_data_tables(self, q):
        """
        Initialise the underlying database tables.
        @param q:           a database cursor to use.
        """
        #
        # INVITE table is the primary table
        #   NAME: identifier of invitee
        #   INVITEUID: UID for this invite
        #   ACCESS: Access mode for share
        #   STATE: Invite response status
        #   SUMMARY: Invite summary
        #
        q.execute(
            """
            create table INVITE (
                INVITEUID      text unique,
                USERID         text unique,
                ACCESS         text,
                STATE          text,
                SUMMARY        text
            )
            """
        )

        q.execute(
            """
            create index USERID on INVITE (USERID)
            """
        )
        q.execute(
            """
            create index INVITEUID on INVITE (INVITEUID)
            """
        )

    def _db_upgrade_data_tables(self, q, old_version):
        """
        Upgrade the data from an older version of the DB.
        """

        # Nothing to do as we have not changed the schema
        pass

    def _makeRecord(self, row):
        
        return Invite(*row)

