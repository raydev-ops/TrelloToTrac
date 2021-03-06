'''
@author: matteo@magni.me
'''

import re
from genshi.builder import tag
from trac.core import *
from trac.web import IRequestHandler
from trac.web.chrome import INavigationContributor, ITemplateProvider, add_warning, add_notice, add_stylesheet
from trac.ticket.api import ITicketChangeListener

import time
from datetime import date, datetime, timedelta
from dateutil import parser
from trac.util.datefmt import parse_date, utc, to_timestamp, to_datetime, \
                              get_date_format_hint, get_datetime_format_hint, \
                              format_date, format_datetime

import trelloclient
import markdowntowiki
import xmlrpc
import json

class TrelloToTracPlugin(Component):

    implements(INavigationContributor, IRequestHandler, ITemplateProvider, ITicketChangeListener)

    __FIELD_NAMES = {
                      'board' : 'Board',
                      'thelist' : 'List',
                      'milestone' : 'Milestone',
                      'iteration' : 'Iteration',
                      'card' : 'Card Number',
                    }
    __FIELD_AGILE = ['board', 'thelist', 'milestone', 'iteration']
    __FIELD = ['board', 'thelist', 'milestone']
    __FIELD_AGILE_SINGLE = ['board', 'card', 'milestone', 'iteration']
    __FIELD_SINGLE = ['board', 'card', 'milestone']


   # INavigationContributor methods
    def get_active_navigation_item(self, req):
        return 'Trello'

    def get_navigation_items(self, req):
        if 'TRAC_ADMIN' in req.perm:
            yield ('mainnav', 'trello',
                tag.a('Trello', href=req.href.trello()))

    # IRequestHandler methods
    def match_request(self, req):
        if 'TRAC_ADMIN' in req.perm:
            match = re.match(r'/trello(?:/(.+))?$', req.path_info)
            if match:
                if match.group(1):
                    req.args['controller'] = match.group(1)
                return True
        else:
            match1 = re.match(r'/trello/webhook', req.path_info)
            match2 = re.match(r'/trello/sendtotrac', req.path_info)
            match3 = re.match(r'/trello/activemilestones', req.path_info)
            if match1:
                req.args['controller'] = 'webhook'
                return True
            elif match2:
                req.args['controller'] = 'sendtotrac'
                return True
            elif match3:
                req.args['controller'] = 'activemilestones'
                return True

    def process_request(self, req):
        controller = self.controller(req.args.get('controller'))
        response = controller(req)
        return response

    # ITemplateProvider methods
    # Used to add the plugin's templates and htdocs
    def get_templates_dirs(self):
        from pkg_resources import resource_filename
        return [resource_filename(__name__, 'templates')]

    def get_htdocs_dirs(self):
        from pkg_resources import resource_filename
        return [('trello', resource_filename(__name__, 'htdocs'))]

    def getUserByTrelloId(self, id):
        user = self.config.get('trello-user', id)
        if len(user) == 0:
            user = None
        return user

    # ITicketChangeListener methods
    # Ticket change hook
    def ticket_created(self, ticket):
        pass

    def ticket_changed(self, ticket, comment, author, old_values):
        if not old_values:
            #get trello conf
            apiKey = self.config.get('trello', 'api_key')
            userAuthToken = self.config.get('trello', 'user_auth_token')
            trello = trelloclient.TrelloClient(apiKey,userAuthToken)
            cardId = self.getCardIdByTicketId(ticket.id)
            if cardId != None:
                card = trelloclient.TrelloCard(trello, cardId)
                card.addComments('[trac] ' + comment)


    def ticket_deleted(self, ticket):
        pass

    def ticket_comment_modified(self, ticket, cdate, author, comment, old_comment):
        pass

    def ticket_change_deleted(self, ticket, cdate, changes):
        pass


    #controllers

    def controller(self, x):
        return {
            'single': self.singleController,
            'webhook': self.webhookController,
            'sendtotrac': self.sendToTracController,
            'activemilestones': self.activeMilestonesController,
            None: self.indexController,
            }[x]

    def indexController(self, req):
        #start db
        db = self.env.get_db_cnx()
        cursor = db.cursor()

        data = {}
        boardId = ''
        listId = ''

        #get trello conf
        apiKey = self.config.get('trello', 'api_key')
        userAuthToken = self.config.get('trello', 'user_auth_token')
        boardList = self.config.getlist('trello', 'boards')
        listList = self.config.getlist('trello', 'lists')

        agileTrac = self.config.getbool('trello', 'agile_trac')
        if agileTrac:
            field_list = TrelloToTracPlugin.__FIELD_AGILE
        else:
            field_list = TrelloToTracPlugin.__FIELD

        estimationTools = self.config.getbool('trello', 'estimationtools')

        #start trello
        trello = trelloclient.TrelloClient(apiKey,userAuthToken)

        #get board,list,milestone lists
        boards = self.getBoardList(boardList, trello)
        lists = self.getListList(listList, trello)
        milestones = self.getActiveMilestone()

        if req.method == 'POST':
            error_msg = None
            for field in field_list:
                value = req.args.get(field).strip()
                if len(value) == 0:
                    error_msg = 'You must fill in the field "' + TrelloToTracPlugin.__FIELD_NAMES[field] + '".'
                    break
                #validate board exist
                if field == 'board':
                    result = self.validateBoardId(value, trello)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        boardId = value
                #validate list exist
                if field == 'thelist':
                    result = self.validateListId(value, trello)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        listId = value
                #validate milestone exist
                if field == 'milestone':
                    result = self.validateMilestone(value)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        milestone = value
                #validate iteration exist
                if field == 'iteration':
                    result = self.validateIteration(value)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        iteration = value
                    break
            if error_msg:
                add_warning(req, error_msg)
                data = req.args
            else:
                #general ticket data
                owner = ''
                version = ''
                severity = 'normale'
                status = 'new'
                resolution = ''
                priority = 'normale'
                keywords = ''
                component = ''
                task = 'task'

                board = trelloclient.TrelloBoard(trello, boardId)
                theList = trelloclient.TrelloList(trello, listId)
                boardInformation = board.getBoardInformation()
                listInformation = theList.getListInformation()

                cards = theList.getCards()
                for c in cards:
                    cardContent = {}
                    cardInformation = c.getCardInformation()

                    #Content
                    cardContent['id'] = cardInformation['id']
                    cardContent['name'] = cardInformation['name']
                    cardContent['url'] = cardInformation['url']

                    if (self.ticketCardExist(cardContent['id'])):
                        error_msg = 'Card "%s" already exists'% cardContent['name']
                    else:
                        #size and name/title
                        if estimationTools:
                            resultSize = self.getSizeByName(cardInformation['name'])
                            size = resultSize['size']
                            cardContent['name'] = resultSize['name']

                        cardId = cardInformation['id']
                        card = trelloclient.TrelloCard(trello, cardId)
                        createCard = card.getCreateCard()

                        #date
                        dt = parser.parse(createCard['actions'][0]['date'])
                        cardContent['timestamp'] = int(time.mktime(dt.timetuple())-time.timezone)


                        #add link to card
                        cardContent['desc'] = '\'\'\'Card Link:\'\'\'[[br]]\n[' + cardContent['url'] + ' vai a Trello] [[br]] \n'
                        #covert desc markdown to trac wiki
                        m2w = markdowntowiki.MarkdownToWiki(cardInformation['desc'])
                        cardContent['desc'] += '[[br]]\'\'\'Description:\'\'\'[[br]]\n'+m2w.convert() + ' [[br]] \n'

                        idMemberCreator = createCard['actions'][0]['idMemberCreator']
                        reporter = self.getUserByTrelloId(idMemberCreator)
                        if reporter is None:
                            reporter = 'trello'
                        members=card.getMembers()

                        # owner
                        owner = self.getFirstMember(members)

                        #cc alla assigned member
                        cc = self.addMembersToCc(members)

                        #checklist
                        checklists = card.getChecklists()
                        cardContent['desc'] = self.addChecklistsToDesc(checklists, cardContent['desc'], trello)

                        #import attachments
                        attachments = card.getAttachments()
                        cardContent['desc'] = self.addAttachmentsToDesc(attachments, cardContent['desc'])

                        #labels
                        labels = cardInformation['labels']
                        cardContent['desc'] = self.addLabelsToDesc(labels, cardContent['desc'])

                        #insert card in ticket
                        try:
                            #id, type, time, changetime, component, severity, priority, owner, reporter, cc, version, milestone, status, resolution, summary, description, keywords
                            cursor.execute("INSERT INTO ticket (id, type, time, changetime, component, severity, priority, owner, reporter, cc, version, milestone, status, resolution, summary, description, keywords) VALUES (DEFAULT, (%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s)) RETURNING id;",[ task, cardContent['timestamp'], cardContent['timestamp'], component , severity, priority, owner, reporter, cc, version, milestone, status, resolution, cardContent['name'], cardContent['desc'], keywords ])
                            idTicket = cursor.fetchone()[0]
                            #comment
                            comments = card.getComments()
                            self.addCommentsToTicket(comments, idTicket)

                            # Attach link to card on trello
                            card.addLinkAttachment(self.getLinkByTicketId(idTicket))
                            # add trellocard id on ticket custom fields
                            self.addTrellocardToTicket(idTicket, cardContent['id'])

                            #add ticket to iteration
                            if agileTrac:
                                self.addTicketToIteration(idTicket,iteration)

                            #add size
                            if estimationTools:
                                estimationToolsField = self.config.get('estimation-tools', 'estimation_field')
                                self.addSizeToTicket(size, estimationToolsField, idTicket, cardContent['timestamp'], reporter)

                        except:
                            db.rollback()
                            raise
                        db.commit()

                        notice_msg='Added card "%s" with id: %s' % (cardContent['name'], idTicket);
                        add_notice(req, notice_msg)

                    if error_msg:
                        add_warning(req, error_msg)
                        error_msg = ''
                data = req.args

        #forever view data
        data['milestone_placeholder'] = 'milestone name'
        data['iteration_placeholder'] = 'iteration number'
        data['agile_trac'] = agileTrac
        data['boards'] = boards
        data['lists'] = lists
        data['milestones'] = milestones
        add_stylesheet(req, 'trello/css/trello.css')

        # This tuple is for Genshi (template_name, data, content_type)
        # Without data the trac layout will not appear.
        return 'trello.html', data, None

    def singleController(self, req):
        #start db
        db = self.env.get_db_cnx()
        cursor = db.cursor()

        data = {}
        boardId = ''
        cardId = ''

        #get trello conf
        apiKey = self.config.get('trello', 'api_key')
        userAuthToken = self.config.get('trello', 'user_auth_token')
        boardList = self.config.getlist('trello', 'boards')

        agileTrac = self.config.getbool('trello', 'agile_trac')
        if agileTrac:
            field_list = TrelloToTracPlugin.__FIELD_AGILE_SINGLE
        else:
            field_list = TrelloToTracPlugin.__FIELD_SINGLE

        estimationTools = self.config.getbool('trello', 'estimationtools')

        #start trello
        trello = trelloclient.TrelloClient(apiKey,userAuthToken)

        #get list of boards
        boards = self.getBoardList(boardList, trello)
        milestones = self.getActiveMilestone()

        if req.method == 'POST':
            error_msg = None
            for field in field_list:
                value = req.args.get(field).strip()
                if len(value) == 0:
                    error_msg = 'You must fill in the field "' + TrelloToTracPlugin.__FIELD_NAMES[field] + '".'
                    break
                #validate board exist
                if field == 'board':
                    result = self.validateBoardId(value, trello)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        boardId = value
                #validate cardid exist
                if field == 'card':
                    result = self.validateCardShortId(value, boardId, trello)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        cardId = result['id']
                #validate milestone exist
                if field == 'milestone':
                    result = self.validateMilestone(value)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        milestone = value
                #validate iteration exist
                if field == 'iteration':
                    result = self.validateIteration(value)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        iteration = value
                    break
            if error_msg:
                add_warning(req, error_msg)
                data = req.args
            else:
                #general ticket data
                owner = ''
                version = ''
                severity = 'normale'
                status = 'new'
                resolution = ''
                priority = 'normale'
                keywords = ''
                component = ''
                task = 'task'

                #get board and card info
                board = trelloclient.TrelloBoard(trello, boardId)
                card = trelloclient.TrelloCard(trello,cardId)

                cardContent = {}
                cardInformation = card.getCardInformation()

                #Content
                cardContent['id'] = cardInformation['id']
                cardContent['name'] = cardInformation['name']
                cardContent['url'] = cardInformation['url']

                if (self.ticketCardExist(cardContent['id'])):
                    error_msg = 'Card "%s" already exists'% cardContent['name']
                else:
                    #size and name/title
                    if estimationTools:
                        resultSize = self.getSizeByName(cardInformation['name'])
                        size = resultSize['size']
                        cardContent['name'] = resultSize['name']

                    createCard = card.getCreateCard()

                    #date
                    dt = parser.parse(createCard['actions'][0]['date'])
                    cardContent['timestamp'] = int(time.mktime(dt.timetuple())-time.timezone)

                    #add link to card
                    cardContent['desc'] = '\'\'\'Card Link:\'\'\'[[br]]\n[' + cardContent['url'] + ' vai a Trello] [[br]] \n'
                    #covert desc markdown to trac wiki
                    m2w = markdowntowiki.MarkdownToWiki(cardInformation['desc'])
                    cardContent['desc'] += '[[br]]\'\'\'Description:\'\'\'[[br]]\n'+m2w.convert() + ' [[br]] \n'

                    idMemberCreator = createCard['actions'][0]['idMemberCreator']
                    reporter = self.getUserByTrelloId(idMemberCreator)
                    if reporter is None:
                        reporter = 'trello'
                    members=card.getMembers()

                    # owner
                    owner = self.getFirstMember(members)

                    # cc alla assigned member
                    cc = self.addMembersToCc(members)

                    # checklist
                    checklists = card.getChecklists()
                    cardContent['desc'] = self.addChecklistsToDesc(checklists, cardContent['desc'], trello)

                    # import attachments
                    attachments = card.getAttachments()
                    cardContent['desc'] = self.addAttachmentsToDesc(attachments, cardContent['desc'])

                    # labels
                    labels = cardInformation['labels']
                    cardContent['desc'] = self.addLabelsToDesc(labels, cardContent['desc'])

                    # insert card in ticket
                    try:
                        # id, type, time, changetime, component, severity, priority, owner, reporter, cc, version, milestone, status, resolution, summary, description, keywords
                        cursor.execute("INSERT INTO ticket (id, type, time, changetime, component, severity, priority, owner, reporter, cc, version, milestone, status, resolution, summary, description, keywords) VALUES (DEFAULT, (%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s)) RETURNING id;",[ task, cardContent['timestamp'], cardContent['timestamp'], component , severity, priority, owner, reporter, cc, version, milestone, status, resolution, cardContent['name'], cardContent['desc'], keywords ])
                        idTicket = cursor.fetchone()[0]
                        # comment
                        comments = card.getComments()
                        self.addCommentsToTicket(comments, idTicket)

                        # Attach link to card on trello
                        card.addLinkAttachment(self.getLinkByTicketId(idTicket))
                        # add trellocard id on ticket custom fields
                        self.addTrellocardToTicket(idTicket, cardContent['id'])

                        # add ticket to iteration
                        if agileTrac:
                            self.addTicketToIteration(idTicket,iteration)
                        # add size
                        if estimationTools:
                            estimationToolsField = self.config.get('estimation-tools', 'estimation_field')
                            self.addSizeToTicket(size, estimationToolsField, idTicket, cardContent['timestamp'], reporter)

                    except:
                        db.rollback()
                        raise
                    db.commit()

                    notice_msg='Added card "%s" with id: %s' % (cardContent['name'], idTicket);
                    add_notice(req, notice_msg)


                if error_msg:
                    add_warning(req, error_msg)
                    data = req.args



        # forever view data
        data['card_placeholder'] = 'card short id'
        data['milestone_placeholder'] = 'milestone name'
        data['iteration_placeholder'] = 'iteration number'
        data['agile_trac'] = agileTrac
        data['boards'] = boards
        data['milestones'] = milestones
        add_stylesheet(req, 'trello/css/trello.css')

        # This tuple is for Genshi (template_name, data, content_type)
        # Without data the trac layout will not appear.
        return 'single.html', data, None

    def webhookController(self, req):
        methods = {
                    'commentCard': self.addCommentByAction
        }
        db = self.env.get_db_cnx()
        cursor = db.cursor()

        data = {}

        # get trello conf
        apiKey = self.config.get('trello', 'api_key')
        userAuthToken = self.config.get('trello', 'user_auth_token')

        data = req.args

        # @TODO check ip
        remote_addr = req.remote_addr
        # if (remote_addr == 'x.x.x.x')

        self.log.debug("Webhook request form ip: %r", remote_addr)
        # request for webhook registration
	if req.get_header('Content-Length') is None or int(req.get_header('Content-Length')) == 0:
            self.log.debug('Content-Length is None or 0')
	else:
            length = int(req.get_header('Content-Length'))
            body = req.read(length)
            self.log.debug('body: %r', body)
            dataResponse = json.loads(str(body))
            #start trello
            trello = trelloclient.TrelloClient(apiKey,userAuthToken)
            action = trelloclient.TrelloWebhookAction(trello, dataResponse['action']['id'])
            action.loadJson(dataResponse)

            if action.type in methods:
                result = methods[action.type](action)
            else:
                self.log.debug('Method %s not implemented', action.type)

        return 'webhook.html', data, None

    def sendToTracController(self, req):
        #start db
        db = self.env.get_db_cnx()
        cursor = db.cursor()

        data = {}
        boardId = ''
        cardId = ''

        #get trello conf
        apiKey = self.config.get('trello', 'api_key')
        userAuthToken = self.config.get('trello', 'user_auth_token')
        boardList = self.config.getlist('trello', 'boards')

        agileTrac = self.config.getbool('trello', 'agile_trac')
        if agileTrac:
            field_list = TrelloToTracPlugin.__FIELD_AGILE_SINGLE
        else:
            field_list = TrelloToTracPlugin.__FIELD_SINGLE

        estimationTools = self.config.getbool('trello', 'estimationtools')

        #start trello
        trello = trelloclient.TrelloClient(apiKey,userAuthToken)

        #get list of boards
        boards = self.getBoardList(boardList, trello)
        milestones = self.getActiveMilestone()

        if req.method == 'GET':
            error_msg = None
            for field in field_list:
                value = req.args.get(field).strip()
                if len(value) == 0:
                    error_msgmsg = 'You must fill in the field "' + TrelloToTracPlugin.__FIELD_NAMES[field] + '".'
                    break
                # validate cardid exist
                if field == 'board':
                    boardId = value
                # validate cardid exist
                if field == 'card':
                    cardId = value
                # validate milestone exist
                if field == 'milestone':
                    result = self.validateMilestone(value)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        milestone = value
                #validate iteration exist
                if field == 'iteration':
                    result = self.validateIteration(value)
                    if not result['res']:
                        error_msg = result['msg']
                        break
                    else:
                        iteration = value
                    break
            if error_msg:
                response = error_msg
            else:
                #general ticket data
                owner = ''
                version = ''
                severity = 'normale'
                status = 'new'
                resolution = ''
                priority = 'normale'
                keywords = ''
                component = ''
                task = 'task'

                #get board and card info
                board = trelloclient.TrelloBoard(trello, boardId)
                card = trelloclient.TrelloCard(trello,cardId)

                cardContent = {}
                cardInformation = card.getCardInformation()

                #Content
                cardContent['id'] = cardInformation['id']
                cardContent['name'] = cardInformation['name']
                cardContent['url'] = cardInformation['url']

                if (self.ticketCardExist(cardContent['id'])):
                    error_msg = 'Card "%s" already exists'% cardContent['name']
                else:
                    #size and name/title
                    if estimationTools:
                        resultSize = self.getSizeByName(cardInformation['name'])
                        size = resultSize['size']
                        cardContent['name'] = resultSize['name']

                    createCard = card.getCreateCard()

                    #date
                    dt = parser.parse(createCard['actions'][0]['date'])
                    cardContent['timestamp'] = int(time.mktime(dt.timetuple())-time.timezone)

                    #add link to card
                    cardContent['desc'] = '\'\'\'Card Link:\'\'\'[[br]]\n[' + cardContent['url'] + ' vai a Trello] [[br]] \n'
                    #covert desc markdown to trac wiki
                    m2w = markdowntowiki.MarkdownToWiki(cardInformation['desc'])
                    cardContent['desc'] += '[[br]]\'\'\'Description:\'\'\'[[br]]\n'+m2w.convert() + ' [[br]] \n'

                    idMemberCreator = createCard['actions'][0]['idMemberCreator']
                    reporter = self.getUserByTrelloId(idMemberCreator)
                    if reporter is None:
                        reporter = 'trello'
                    members=card.getMembers()

                    # owner
                    owner = self.getFirstMember(members)

                    # cc alla assigned member
                    cc = self.addMembersToCc(members)

                    # checklist
                    checklists = card.getChecklists()
                    cardContent['desc'] = self.addChecklistsToDesc(checklists, cardContent['desc'], trello)

                    # import attachments
                    attachments = card.getAttachments()
                    cardContent['desc'] = self.addAttachmentsToDesc(attachments, cardContent['desc'])

                    # labels
                    labels = cardInformation['labels']
                    cardContent['desc'] = self.addLabelsToDesc(labels, cardContent['desc'])

                    # insert card in ticket
                    try:
                        # id, type, time, changetime, component, severity, priority, owner, reporter, cc, version, milestone, status, resolution, summary, description, keywords
                        cursor.execute("INSERT INTO ticket (id, type, time, changetime, component, severity, priority, owner, reporter, cc, version, milestone, status, resolution, summary, description, keywords) VALUES (DEFAULT, (%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s),(%s)) RETURNING id;",[ task, cardContent['timestamp'], cardContent['timestamp'], component , severity, priority, owner, reporter, cc, version, milestone, status, resolution, cardContent['name'], cardContent['desc'], keywords ])
                        idTicket = cursor.fetchone()[0]
                        # comment
                        comments = card.getComments()
                        self.addCommentsToTicket(comments, idTicket)

                        # Attach link to card on trello
                        card.addLinkAttachment(self.getLinkByTicketId(idTicket))
                        # add trellocard id on ticket custom fields
                        self.addTrellocardToTicket(idTicket, cardContent['id'])

                        # add ticket to iteration
                        if agileTrac:
                            self.addTicketToIteration(idTicket,iteration)
                        # add size
                        if estimationTools:
                            estimationToolsField = self.config.get('estimation-tools', 'estimation_field')
                            self.addSizeToTicket(size, estimationToolsField, idTicket, cardContent['timestamp'], reporter)

                    except:
                        db.rollback()
                        raise
                    db.commit()

                    notice_msg='Added card "%s" with id: %s' % (cardContent['name'], idTicket);
                    response = notice_msg

                if error_msg:
                    response = error_msg

        req.send_response(200)
        req.send_header('Content-Type', 'application/json')
        req.send_header('Content-Length', len(response))
        req.end_headers()
        req.write(response)


    def activeMilestonesController(self, req):
        # response = '''{"milestones": ["prima","seconda"]}'''
        response = json.dumps(self.getActiveMilestone())

        req.send_response(200)
        req.send_header('Content-Type', 'application/json')
        req.send_header('Content-Length', len(response))
        req.end_headers()
        req.write(response)

    def validateMilestone(self, milestone):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM milestone WHERE name LIKE %s"
        cursor.execute(sql, [milestone])
        row = cursor.fetchone()
        if row is None:
            return {'res':False, 'msg':'Milestone is not exist.'}
        else:
            return {'res':True, 'milestone' : row}

    def validateIteration(self, iteration):
        s = iteration
        u = unicode(s)
        if not u.isnumeric():
            return {'res':False, 'msg':'Iteration must be a number.'}
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM iteration WHERE id=%s"
        cursor.execute(sql, [iteration])
        row = cursor.fetchone()
        if row is None:
            return {'res':False, 'msg':'Iteration is not exist.'}
        else:
            return {'res':True}

    def validateCardId(self, cardId, trello):
        result = trello.cardExist(cardId)
        if result['res']:
            return {'res':True}
        else:
            return {'res':False, 'msg':'Card is not exist.'}

    # validate cardShortId in board
    def validateCardShortId(self, shortId, boardId, trello):
        result = trello.cardShortIdExist(shortId,boardId)
        if result['res']:
            return {'res':True, 'id':result['id']}
        else:
            return {'res':False, 'msg':'Card is not exist.'}

    # validate boardId
    def validateBoardId(self, boardId, trello):
        result = trello.boardExist(boardId)
        if result['res']:
            return {'res':True}
        else:
            return {'res':False, 'msg':'Board is not exist.'}

    # validate listId
    def validateListId(self, listId, trello):
        result = trello.listExist(listId)
        if result['res']:
            return {'res':True, 'boardId': result['boardId']}
        else:
            return {'res':False, 'msg':'List is not exist.'}

    def addTicketToIteration(self, idTicket, idIteration):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("INSERT INTO iteration_ticket VALUES ((%s),(%s))",[ idIteration, idTicket ])

    def addMembersToCc(self, members):
        cc=''
        count = 1
        l = len(members)
        for m in members:
            tracUser = self.getUserByTrelloId(m['id'])
            cc += tracUser
            if count < l :
                cc += ','
            count += 1
        return cc

    def addChecklistsToDesc(self, checklists, desc, trello):
        if len(checklists):
            desc += '[[br]] \n\'\'\'Checklists:\'\'\' [[br]]\n'
            for c in checklists:
                checklist = trelloclient.TrelloChecklist(trello, c)
                checklist = checklist.getChecklistInformation()
                desc += '\'\'' + checklist['name'] + '\'\' [[br]]\n'
                for item in checklist['checkItems']:
                    desc += ' * ' + item['name'] + '\n'
        return desc

    def addAttachmentsToDesc(self, attachments, desc):
        if len(attachments):
            desc += '[[br]] \n\'\'\'Attachments:\'\'\' [[br]]\n\'\''
            for a in attachments:
                desc += '[' + a['url'] + ' '  + a['name'] + ']\'\' [[br]]\n'
        return desc

    def addLabelsToDesc(self, labels, desc):
        if len(labels):
            desc += '[[br]] \n\'\'\'Label:\'\'\' [[br]]\n'
            for l in labels:
                if l['name'] == '':
                    desc += '\'\'' + l['color'] + '\'\' [[br]]\n'
                else:
                    desc += '\'\'' + l['color'] + ': ' + l['name'] + '\'\' [[br]]\n'
        return desc

    def addCommentsToTicket(self, comments, idTicket):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        for c in comments:
            dtComment = parser.parse(c['date'])
            timestamp = int(time.mktime(dtComment.timetuple())-time.timezone)
            userComment = self.getUserByTrelloId(c['idMemberCreator'])
            m2w = markdowntowiki.MarkdownToWiki(c['data']['text']).convert()
            cursor.execute("INSERT INTO ticket_change VALUES ((%s),(%s),(%s),(%s),(%s),(%s))",[idTicket,timestamp,userComment,'comment', '', m2w])

    def addSizeToTicket(self, size, estimationToolsField, idTicket, timestamp, creator):
        if size is not None:
            db = self.env.get_db_cnx()
            cursor = db.cursor()
            cursor.execute("INSERT INTO ticket_custom VALUES ((%s),(%s),(%s))",[idTicket,estimationToolsField,size])
            #if you want to have history
            #cursor.execute("INSERT INTO ticket_change VALUES ((%s),(%s),(%s),(%s),(%s),(%s))",[idTicket,timestamp,creator,estimationToolsField, '',size])

    def getSizeByName(self, name):
        if re.search(r"^\((\d+)\) ", name):
            size = re.search(r"^\((\d+)\) ", name).group(1)
            name = re.sub(r"^\((\d+)\) ", '', name)
            return {'name':name, 'size': size}
        else:
            return {'name':name, 'size': None}


    def getBoardList(self, boardList, trello):
        boards = []
        for bId in boardList:
            b = trelloclient.TrelloBoard(trello, bId).getBoardInformation()
            board = {}
            board['id'] = b['id']
            board['name'] = b['name']
            boards.append(board)
        return boards

    def getListList(self, listList, trello):
        lists = []
        for lId in listList:
            l = trelloclient.TrelloList(trello, lId).getListInformation()
            lis = {}
            lis['id'] = l['id']
            lis['name'] = l['name']
            lists.append(lis)
        return lists

    def getActiveMilestone(self):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT name FROM milestone WHERE completed = 0 ORDER BY name ASC"
        cursor.execute(sql)
        milestones = cursor.fetchall()
        return milestones

    def getFirstMember(self, members):
        if len(members) > 0:
            owner = members.pop(0)
            return self.getUserByTrelloId(owner['id'])
        else:
            return None

    def addTrellocardToTicket(self, idTicket, cardId):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("INSERT INTO ticket_custom (ticket, name, value) VALUES ((%s), 'trellocard', (%s));", [idTicket, cardId ])

    def getLinkByTicketId(self, idTicket):
        host = self.config.get('project', 'url')
        link = host + 'ticket/' + str(idTicket)
        return link

    def getTicketIdByCardId(self, cardId):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT ticket FROM ticket_custom WHERE name = 'trellocard' AND value LIKE %s"
        cursor.execute(sql, [cardId])
        row = cursor.fetchone()
        if row is None:
            return None
        else:
            return row[0]

    def getCardIdByTicketId(self, ticketId):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT value FROM ticket_custom WHERE name = 'trellocard' AND ticket = %s"
        cursor.execute(sql, [ticketId])
        row = cursor.fetchone()
        if row is None or row[0] == '':
            return None
        else:
            return row[0]

    def addCommentToTicket(self, comment, idTicket):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        dtComment = parser.parse(comment['date'])
        timestamp = int(time.mktime(dtComment.timetuple())-time.timezone)
        userComment = self.getUserByTrelloId(comment['idMemberCreator'])
        m2w = markdowntowiki.MarkdownToWiki(comment['data']['text']).convert()
        cursor.execute("INSERT INTO ticket_change VALUES ((%s),(%s),(%s),(%s),(%s),(%s))",[idTicket,timestamp,userComment,'comment', '', m2w])

    def addCommentByAction(self, action):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        idTicket = self.getTicketIdByCardId(action.data['card']['id'])
        if idTicket != None and not self.isCommentFromTrac(action.data['text']):
            comment = {}
            comment['date'] = action.date
            comment['idMemberCreator'] = action.idMemberCreator
            comment['data'] = action.data
            # add trello prefix to comment text
            comment['data']['text'] = '[trello] ' + comment['data']['text']
            try:
                self.addCommentToTicket(comment, idTicket)
            except:
                db.rollback()
                raise
            db.commit()
            return True
        else:
            return False

    def isCommentFromTrac(self, text):
        return text.startswith('[trac]')

    def ticketCardExist(self, cardId):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT ticket FROM ticket_custom WHERE name = 'trellocard' AND value LIKE %s"
        cursor.execute(sql, [cardId])
        row = cursor.fetchone()
        if row is None:
            return False
        else:
            return True

