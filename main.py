#!/usr/bin/python
# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup, NavigableString
import regex as re
from weibo import Client
from weibo_settings import *
import os
import imgkit
import logging
from retrying import retry
import queue
from apscheduler.schedulers.blocking import BlockingScheduler
import pickle
import json
from collections import OrderedDict
import hashlib
from datetime import datetime
import time

logging.basicConfig(level=logging.WARNING,
                    format='%(asctime)s %(filename)s[line:%(lineno)d] [%(levelname)s] %(message)s',
                    datefmt='%a, %d %b %Y %H:%M:%S',
                    filename='sakugawikibot.log',
                    filemode='a')

UPDATE_LIST_URL = 'https://www18.atwiki.jp/_ajax/setplugin/sakuga'
DIFF_URL_TEMPLET = 'https://www18.atwiki.jp/sakuga/diffx/{0}.html'
g_scheduler = None


class ChangedContent:

    def __init__(self, text, index, type_):
        self.text = text
        self.index = index
        self.type = type_


class WorkingData:

    def __init__(self):
        self.queue_to_work = queue.Queue()  # 参考数据格式：{'pagename': '平山智', 'pageid': 1080, 'old': '1h', 'modify': ''}
        self.post_queue = queue.Queue()
        if os.path.exists('least_data'):
            with open('least_data', 'rb') as file:
                self.least_data = pickle.load(file)
        else:
            self.least_data = {}

    def save_least_data(self):
        with open('least_data', 'wb') as file:
            pickle.dump(self.least_data, file)


def get_changed_content(diff) -> list:
    changes = []
    count = 1
    for line in diff.children:
        if isinstance(line, NavigableString):
            if len(line.strip()) > 2:
                count, index = count + 1, count
            continue
        count, index = count + 1, count

        if 'style' in line.attrs.keys():
            if line['style'] == 'color:red;':
                changed = ChangedContent(line.string, index, 'del')
                changes.append(changed)
            elif line['style'] == 'color:blue;':
                changed = ChangedContent(line.string, index, 'add')
                changes.append(changed)
    return changes


@retry(stop_max_attempt_number=5,
       stop_max_delay=600000,
       wait_fixed=30000)
def get_diff_soup(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'lxml')
    # title = soup.head.title.string
    # name = re.search(r'\[(.+)\]', title).groups()[0]
    diff = soup.find('pre', class_='diff')
    return diff


def get_md5_of_diff(url=None, pageid=None):
    if pageid is not None:
        url = DIFF_URL_TEMPLET.format(pageid)
    diff = get_diff_soup(url)
    md5 = hashlib.md5()
    md5.update(str(diff).encode())
    return md5.hexdigest()


def to_weibo_text(name: str, changes: list, url: str) -> str:
    def area_to_str(add_area, del_area):
        result = ''
        if del_area and add_area:
            longest = len(del_area) if len(del_area) < len(add_area) else len(add_area)
            for i in range(longest):  # 去除无文本修改项
                if re.sub('\s', '', del_area[i].text) == re.sub('\s', '', add_area[i].text):
                    del_area[i].text, add_area[i].text = '', ''
            for d in del_area:
                if d.text != '':
                    result += '-' + d.text + '\n'
            for a in add_area:
                if a.text != '':
                    result += '+' + a.text + '\n'
        elif del_area:
            for d in del_area:
                if d.text != '':
                    result += '-' + d.text + '\n'
        elif add_area:
            for a in add_area:
                if a.text != '':
                    result += '+' + a.text + '\n'
        add_area.clear()
        del_area.clear()
        if line.type == 'add':
            add_area.append(line)
        else:
            del_area.append(line)
        return result

    prue_changes = []
    for changed in changes:
        t = re.sub(r'<[^<>]*>', '', str(changed.text)).strip()
        if t != '':
            prue_changes.append(ChangedContent(t, changed.index, changed.type))
    del_area = []
    add_area = []
    flags = ''
    if_end = False
    result = ''
    last_index = len(prue_changes) - 1
    for index, line in enumerate(prue_changes):
        if line.type == flags or flags == '':  # 如果类型无改变或初始化
            if_end = False
            if flags == '':
                flags = line.type
            if flags == 'del':
                del_area.append(line)
            else:
                add_area.append(line)
        else:
            if flags == 'del':  # del -> add
                if line.index - del_area[0].index == len(del_area):  # 为连续更改区时
                    if_end = False
                    add_area.append(line)
                else:
                    if_end = True
                flags = 'add'
            elif flags == 'add':  # add -> del
                if_end = True
                flags = 'del'

            if if_end:
                result += area_to_str(add_area, del_area)
        if index == last_index:
            result += area_to_str(add_area, del_area)

    text = name + '：' + result
    text = re.sub('\n', '|', text.strip())
    if len(text) < 130:
        return text + url
    else:
        text = text[:127] + '……'
        return text + url


def make_diff_pic(filepath, diff, name):
    pic_html = '<meta charset="UTF-8" /><div style="background:#f8f3e6"><font face="sans-serif">'
    pic_html += '<h3>「' + name + '」的最新版变更点</h3>'
    for line in diff.children:
        if isinstance(line, NavigableString):
            if len(str(line).strip()) > 2:
                pic_html += '<br />    …………    <br /><br />'
        elif 'style' in line.attrs.keys():
            punc = '+' if line['style'] == 'color:blue;' else '- '
            pic_html += punc + line.__str__() + '<br />'
    pic_html += '</font></div>'
    try:
        imgkit.from_string(pic_html, filepath)
    except Exception as e:
        logging.exception('构建图片出错')
        raise e


@retry(stop_max_attempt_number=5,
       wait_fixed=30000)
def post_weibo(text, pic):
    try:
        weibo_bot = Client(APP_KEY, APP_SECRET, REDIRECT_URI, username=USER_NAME, password=PASSWORD)
        with open(pic, 'rb') as img:
            weibo_bot.post('statuses/share', status=text, pic=img)
        # print('微博已发送')
    except Exception as e:
        if e.args[0] != '20016 update weibo too fast!':
            logging.exception('发送微博失败:')
        if '10023' in e.args[0]:
            g_scheduler.pause()
            logging.warning('任务暂停')
            time.sleep(60 * 60 * 2)  # 触发接口频次限制时沉睡两小时
            g_scheduler.resume()
            logging.info('任务继续运行')
        raise e


def update_urls_to_push(working_data: WorkingData):
    data = (('recent[atwiki_plugin_recent_1ac5d0fca5e1e693fa60992b521d112c][num]', 100),
            ('recent[atwiki_plugin_recent_1ac5d0fca5e1e693fa60992b521d112c][modify]', 'none'))
    headers = {'content-type': 'application/x-www-form-urlencoded; charset=UTF-8'}
    all_list = json.loads(requests.post(UPDATE_LIST_URL, data=data, headers=headers).text,
                          object_pairs_hook=OrderedDict)  # 以文本顺序读取以保持内部数据为时间顺序
    new_least = {}
    for index, day in enumerate(all_list['recent']['atwiki_plugin_recent_1ac5d0fca5e1e693fa60992b521d112c']):
        end_flag = False
        if index > 1:  # 只获取今天和昨天的数据
            break
        time_ratio = {
            's': 1,
            'm': 60,
            'h': 3600,
            'd': 3600 * 24
        }
        for new_update in all_list['recent']['atwiki_plugin_recent_1ac5d0fca5e1e693fa60992b521d112c'][day]:
            old = re.search(r'(\d+)([a-z])', new_update['old']).groups()
            update_time = int(old[0]) * time_ratio[old[1]]
            if len(new_least) <= 3:
                md5 = get_md5_of_diff(pageid=new_update['pageid'])
                new_least[new_update['pageid']] = md5
            if update_time > 5 * time_ratio['h']:  # 只获取5h内的数据
                end_flag = True
                if len(new_least) < 3:
                    continue
                else:
                    break
            if new_update['pageid'] in working_data.least_data:  # 遇上重复页面时检测页面是否有新变化
                md5 = get_md5_of_diff(pageid=new_update['pageid'])
                if md5 == working_data.least_data[new_update['pageid']]:
                    end_flag = True
                    if len(new_least) < 3:
                        continue
                    else:
                        break
            working_data.queue_to_work.put(new_update)
        working_data.least_data = new_least
        working_data.save_least_data()
        if end_flag:
            break


def pics_clear_task():
    pic_path = os.path.join('.', 'pics')
    try:
        for root, dirs, names in os.walk(pic_path):
            for file in names:
                os.remove(file)
    except Exception as e:
        logging.exception(e)


def weibo_post_task(working_data: WorkingData):
    if working_data.post_queue.empty():
        return
    while not working_data.post_queue.empty():
        weibo = working_data.post_queue.get()
        post_weibo(weibo['text'], weibo['pic'])
        time.sleep(20)  # 避免接口频次限制
    print(str(datetime.now()) + '微博发送完毕')


def gene_task(task_data, working_data):
    try:
        url = DIFF_URL_TEMPLET.format(task_data['pageid'])
        diff = get_diff_soup(url)
        name = task_data['pagename']
        changes = get_changed_content(diff)
        text = to_weibo_text(name, changes, url)
        dt = datetime.now()
        pic_name = dt.strftime('%Y-%m-%d_%H-%M-%S-%f') + '.jpg'
        pic = os.path.join('.', 'pics', pic_name)
        make_diff_pic(pic, diff, name)
        weibo = {'text': text,
                 'pic': pic}
        working_data.post_queue.put(weibo)
        # post_weibo(text, pic)
    except Exception as e:
        working_data.queue_to_work.put(task_data)
        raise e


def tasks(working_data: WorkingData):
    print(str(datetime.now()) + ' tasks run')
    try:
        update_urls_to_push(working_data)
        while not working_data.queue_to_work.empty():
            task_data = working_data.queue_to_work.get()
            # t = threading.Thread(target=gene_task, args=(task_data, working_data))
            # t.start()
            gene_task(task_data, working_data)
        weibo_post_task(working_data)
    except:
        logging.exception('运行错误')


def tasks_run():
    working_data = WorkingData()
    scheduler = BlockingScheduler()
    global g_scheduler
    g_scheduler = scheduler.add_job(tasks, 'interval', args=[working_data], hours=1, minutes=2)
    scheduler.add_job(pics_clear_task, 'cron', hour=4, day_of_week=3)
    scheduler.start()


if __name__ == '__main__':
    tasks_run()
