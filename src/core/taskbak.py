from __future__ import absolute_import, unicode_literals
import logging
import functools
import threading
from django.http import HttpResponse
from libs import util
from libs import send_email
from libs import call_inception
from .models import (
    Usermessage,
    DatabaseList,
    Account,
    globalpermissions,
    SqlOrder,
    SqlRecord,
    grained
)
CUSTOM_ERROR = logging.getLogger('Yearning.core.views')


def grained_permissions(func):
    '''

    :argument 装饰器函数,校验细化权限。非法请求直接返回401交由前端判断状态码

    '''
    @functools.wraps(func)
    def wrapper(self, request, args=None):
        if request.method == "PUT" and args != 'connection':
            return func(self, request, args)
        else:
            if request.method == "GET":
                permissions_type = request.GET.get('permissions_type')
            else:
                permissions_type = request.data['permissions_type']
            user = grained.objects.filter(username=request.user).first()
            if user is not None and user.permissions[permissions_type] == '1':
                return func(self, request, args)
            else:
                return HttpResponse(status=401)
    return wrapper


class order_push_message(threading.Thread):

    '''

    :argument 同意执行工单调用该方法异步处理数据

    '''

    def __init__(self, addr_ip, id, from_user, to_user):
        super().__init__()
        self.id = id
        self.addr_ip = addr_ip
        self.order = SqlOrder.objects.filter(id=id).first()
        self.from_user = from_user
        self.to_user = to_user
        self.title = f'工单:{self.order.work_id}审核通过通知'
        self.statue = 'normal'

    def run(self):
        self.execute()
        self.agreed()

    def execute(self):

        '''

        :argument 将获得的sql语句提交给inception执行并将返回结果写入SqlRecord表,最后更改该工单SqlOrder表中的status

        :param
                self.order
                self.id

        :return: none

        '''

        detail = DatabaseList.objects.filter(id=self.order.bundle_id).first()

        with call_inception.Inception(
                LoginDic={
                    'host': detail.ip,
                    'user': detail.username,
                    'password': detail.password,
                    'db': self.order.basename,
                    'port': detail.port
                }
        ) as f:
            res = f.Execute(sql=self.order.sql, backup=self.order.backup)
            SqlOrder.objects.filter(id=self.id).update(status=1)
            for i in res:
                if i['stagestatus'].find('failed') != -1:
                    self.statue = 'bad'
                SqlRecord.objects.get_or_create(
                    date=util.date(),
                    state=i['stagestatus'],
                    sql=i['sql'],
                    area=detail.computer_room,
                    name=detail.connection_name,
                    error=i['errormessage'],
                    base=self.order.basename,
                    workid=self.order.work_id,
                    person=self.order.username,
                    reviewer=self.order.assigned,
                    affectrow=i['affected_rows'],
                    sequence=i['sequence'],
                    backup_dbname=i['backup_dbname'],
                    execute_time=i['execute_time'],
                    SQLSHA1=i['SQLSHA1']
                )

    def agreed(self):

        '''

        :argument 将执行的结果通过站内信,email,dingding 发送

        :param   self.from_user
                 self.to_user
                 self.title
                 self.order
                 self.addr_ip

        :return: none

        '''

        Usermessage.objects.get_or_create(
            from_user=self.from_user, time=util.date(),
            title=self.title, content='该工单已审核通过!', to_user=self.to_user,
            state='unread'
        )

        content = DatabaseList.objects.filter(id=self.order.bundle_id).first()
        mail = Account.objects.filter(username=self.to_user).first()
        tag = globalpermissions.objects.filter(authorization='global').first()

        if tag is None or tag.dingding == 0:
            pass
        else:
            try:
                if content.url:
                    if self.statue == 'normal':
                        util.dingding('# 【<font face=\"微软雅黑\">工单执行通知</font>】 \n #  \n <br>  \n  **工单编号:**  %s \n  \n  **发起人员:**  <font color=\"#000080\">%s</font><br /> \n \n  **审核人员:**  <font color=\"#000080\">%s</font><br /> \n \n **平台地址:**  http://%s \n  \n **工单备注:**  %s \n \n **执行状态:**  <font color=\"#38C759\">已执行</font><br /> \n \n **执行结果:**  <font color=\"#38C759\">正常</font><br /> \n \n **业务备注:**  %s \n '
                                % (self.order.work_id, self.order.username, self.from_user,self.addr_ip, self.order.text, content.after), url=content.url)
                    else:
                        util.dingding(
                            '# 【<font face=\"微软雅黑\">工单执行通知</font>】 \n #  \n <br>  \n  **工单编号:**  %s \n  \n  **发起人员:**  <font color=\"#000080\">%s</font><br /> \n \n  **审核人员:**  <font color=\"#000080\">%s</font><br /> \n \n **平台地址:**  http://%s \n  \n **工单备注:**  %s \n \n **执行状态:**  <font color=\"#38C759\">已执行</font><br /> \n \n **执行结果:**  <font color=\"#38C759\">正常</font><br /> \n \n **业务备注:**  %s \n '
                            % (self.order.work_id, self.order.username, self.from_user, self.addr_ip, self.order.text,
                               content.after), url=content.url)
            except Exception as e:
                CUSTOM_ERROR.error(f'{e.__class__.__name__}--钉钉推送失败: {e}')

        if tag is None or tag.email == 0:
            pass
        else:
            try:
                if mail.email:
                    mess_info = {
                        'workid': self.order.work_id,
                        'to_user': self.order.username,
                        'addr': self.addr_ip,
                        'text': self.order.text,
                        'note': content.after}
                    put_mess = send_email.send_email(to_addr=mail.email)
                    put_mess.send_mail(mail_data=mess_info, type=0)
            except Exception as e:
                CUSTOM_ERROR.error(f'{e.__class__.__name__}--邮箱推送失败: {e}')


class rejected_push_messages(threading.Thread):

    '''

    :argument 驳回工单调用该方法异步处理数据

    '''

    def __init__(self, _tmpData, to_user,from_user, addr_ip, text):
        super().__init__()
        self.to_user = to_user
        self.from_user = from_user
        self._tmpData = _tmpData
        self.addr_ip = addr_ip
        self.text = text

    def run(self):
        self.execute()

    def execute(self):

        '''

        :argument 更改该工单SqlOrder表中的status

        :param
                self._tmpData
                self.addr_ip
                self.text
                self.to_user

        :return: none

        '''
        content = DatabaseList.objects.filter(id=self._tmpData['bundle_id']).first()
        mail = Account.objects.filter(username=self.to_user).first()
        tag = globalpermissions.objects.filter(authorization='global').first()
        if tag is None or tag.dingding == 0:
            pass
        else:
            try:
                if content.url:
                    util.dingding('# 【<font face=\"微软雅黑\">工单驳回通知</font>】 \n #  \n <br>  \n  **工单编号:**  %s \n  \n  **发起人员:**  <font color=\"#000080\">%s</font><br /> \n \n **审核人员:**  <font color=\"#000080\">%s</font><br /> \n \n **平台地址:**  http://%s \n  \n **驳回说明:**  %s \n \n **状态:**  驳回 \n "'
                                % (self._tmpData['work_id'], self.to_user,self.from_user, self.addr_ip, self.text), url=content.url)
            except Exception as e:
                CUSTOM_ERROR.error(f'{e.__class__.__name__}--钉钉推送失败: {e}')
        if tag is None or tag.email == 0:
            pass
        else:
            try:
                if mail.email:
                    mess_info = {
                        'workid': self._tmpData['work_id'],
                        'to_user': self.to_user,
                        'addr': self.addr_ip,
                        'rejected': self.text}
                    put_mess = send_email.send_email(to_addr=mail.email)
                    put_mess.send_mail(mail_data=mess_info, type=1)
            except Exception as e:
                CUSTOM_ERROR.error(f'{e.__class__.__name__}--邮箱推送失败: {e}')


class submit_push_messages(threading.Thread):

    '''

    :argument 提交工单调用该方法异步处理数据

    '''

    def __init__(self, workId, user,to_user, addr_ip, text, assigned, id):
        super().__init__()
        self.workId = workId
        self.user = user
        self.to_user = to_user
        self.addr_ip = addr_ip
        self.text = text
        self.assigned = assigned
        self.id = id

    def run(self):
        self.submit()

    def submit(self):
        '''

        :argument 更改该工单SqlOrder表中的status

        :param
                self.workId
                self.user
                self.addr_ip
                self.text
                self.assigned
                self.id
        :return: none

        '''
        content = DatabaseList.objects.filter(id=self.id).first()
        mail = Account.objects.filter(username=self.assigned).first()
        tag = globalpermissions.objects.filter(authorization='global').first()
        if tag is None or tag.dingding == 0:
            pass
        else:
            if content.url:
                try:
                    util.dingding('# 【<font face=\"微软雅黑\">工单提交通知</font>】 \n #  \n <br>  \n  **工单编号:**  %s \n  \n  **提交人员:**  <font color=\"#000080\">%s</font><br /> \n \n **审核人员:**  <font color=\"#000080\">%s</font><br /> \n \n**平台地址:**  http://%s \n  \n **工单备注:**  %s \n \n **执行状态:**  <font color=\"#38C759\">已提交</font><br /> \n \n **执行状态:**  <font color=\"#38C759\">已提交</font><br /> \n \n **业务备注:**  %s \n '
                                % (self.workId, self.user, self.to_user,self.addr_ip, self.text, content.before), url=content.url)
                except Exception as e:
                    CUSTOM_ERROR.error(f'{e.__class__.__name__}--钉钉推送失败: {e}')
        if tag is None or tag.email == 0:
            pass
        else:
            if mail.email:
                mess_info = {
                    'workid': self.workId,
                    'to_user': self.user,
                    'addr': self.addr_ip,
                    'text': self.text,
                    'note': content.before}
                try:
                    put_mess = send_email.send_email(to_addr=mail.email)
                    put_mess.send_mail(mail_data=mess_info, type=2)
                except Exception as e:
                    CUSTOM_ERROR.error(f'{e.__class__.__name__}--邮箱推送失败: {e}')