# -*- coding: utf-8 -*-

import socket
import threading
import sys
import traceback
import subprocess
import fcntl
import os
import time
import select
import struct
import urllib2
import urllib
import base64
import signal
import requests

"""
本程序使用epoll来获取网络和进程输出管道内容
connections：key是连接的fd，value是客户端的sock对象。有新连接时创建，客户端主动断开、或者任务执行完毕后销毁
clientIn：key是连接的fd，value是客户端向我发的内容。客户端向我发东西古来，我会先放到这里。交给相应程序处理完就清除
clientOut：key是连接的fd，value是即将向客户端发送的内容。发了就清掉
cmdOut：key是外部程序的stdout的fd，value是stdout的一个tuple，内容是（stdout对象，调用者的连接fd）。执行外部程序后，外部程序的stdout和stderr内容就靠操作stdout对象来获得
processes：key是连接的fd，value是程序subprocess.Popen对象
runningCmds: key是用户名，value是FUNCS中的命令key
"""

epoll = select.epoll()
connections =   {}
clientIn =      {}
clientOut =     {}
cmdOut =        {}
processes =     {}
runningCmds =    {}
destroySet = set()
noLimitCmds = set(["showlog", "getstatus"])

LISTEN_PORT = ("0.0.0.0", 6666)
FUNCS = {
    "stop_command"      : ("停止正在执行的任务。会特殊处理",    None),
    "start_server"      : ("正常启动私服",      "~/tools/start_server.sh"),
    "stop_server"       : ("正常停服",          "~/tools/stop_server.sh"),
    "showlog"           : ("跟踪log内容",       "~/tools/showlog.sh"),
    "getstatus"         : ("获得私服状态，版本，分支",    "~/tools/getstatus.sh"),
}

def usage (fd, errorDesc):
    out = [errorDesc]
    out.append("\n格式: <私服名> <命令>\n")
    out.append("例如: zfy showlog\n")
    out.append("可用命令：\n")
    for _cmd, (desc, _ ) in FUNCS.iteritems():
        out.append("%s:\t%s\n"%(desc, _cmd))
    clientOut[fd].extend(out)

def log(*msg):
    print("[%s]%s"%(time.strftime("%Y-%m-%d %H:%M:%S"), " ".join((str(m) for m in msg))))
    sys.stdout.flush()

def getIp(ifname="eth0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack('256s', ifname[:15]))[20:24])

def sendNotice(to, msg):
    localIP = getIp()
    msg = "%s\r\n%s"%(localIP, msg)
    data = {
        "receiver":to,
        "msg":msg
    }
    ## 等你来实现

def dealCmd(data, fileno):
    data = data.strip()
    cmdList = data.split(" ")
    cmdLen = len(cmdList)
    if cmdLen < 2:
        usage(fileno, "参数错误，需要至少2个参数：私服名和命令")
        destroySet.add(fileno)
        return
    elif cmdList[1] not in FUNCS:
        usage(fileno, "参数错误，找不到%s对应的命令"%cmdList[1])
        destroySet.add(fileno)
        return

    who = cmdList[0]
    funcCmd = cmdList[1]
    if funcCmd == "stop_command":
        stopCmd(fileno)
        destroySet.add(fileno)
        return

    if who in runningCmds:
        runningCmd = runningCmds[who]
        if not funcCmd in noLimitCmds:
            clientOut[fileno].extend("上一个程序[%s]还没执行完毕。如果要强制停止，请使用stop_command\n"%runningCmd)
            destroySet.add(fileno)
            return

    desc, command = FUNCS[funcCmd]

    cmds = "su %s -c '%s'"%(who, command)
    if cmdLen > 2:
        cmds = "su %s -c '%s %s'"%(who, command, " ".join(cmdList[2:]))


    log("(%d)command: %s"%(fileno, data))
    p = subprocess.Popen(cmds, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, bufsize=1, shell=True, close_fds=True)
    ## 记录一下艰辛：
    ## shell=True，才可以用su -c的方式来执行
    ## close_fds=True，才可以在执行完命令后，不用等这个命令创建的子进程结束就退出，比如起服务器
    stdoutFd = p.stdout.fileno()
    epoll.register(stdoutFd, select.EPOLLIN|select.EPOLLERR|select.EPOLLHUP)
    fl = fcntl.fcntl(stdoutFd, fcntl.F_GETFL)
    fcntl.fcntl(stdoutFd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    cmdOut[stdoutFd] = (p.stdout, fileno, who)
    processes[fileno] = p
    if funcCmd not in noLimitCmds:
        runningCmds[who] = funcCmd

def stopCmd(clientFd):
    if clientFd in cmdOut:
        stopCmd(cmdOut[clientFd][1])

    elif clientFd in processes:
        p = processes[clientFd]
        stdoutfp = p.stdout.fileno()
        epoll.unregister(stdoutfp)
        stdout, pfileno, who = cmdOut[stdoutfp]
        stdout.close()
        del cmdOut[stdoutfp]
        p.terminate()
        p.wait()

        del processes[clientFd]
        if who in runningCmds:
            del runningCmds[who]

def destroy (clientFd):
    if not clientFd in connections:
        return
    log("(%d)disconnect"%clientFd)
    epoll.unregister(clientFd)
    connections[clientFd].close()
    del connections[clientFd]
    del clientIn[clientFd]
    del clientOut[clientFd]
    destroySet.discard(clientFd)
    stopCmd(clientFd)

def main():
    serverSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serverSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    serverSocket.bind(LISTEN_PORT)
    serverSocket.listen(2)
    serverSocket.setblocking(0)
    serverSocket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    epoll.register(serverSocket.fileno(), select.EPOLLIN)
    try:
        while True:
            events = epoll.poll(1)
            for fileno, event in events:
                if fileno == serverSocket.fileno():
                    client, address = serverSocket.accept()
                    client.setblocking(0)
                    fd = client.fileno()
                    log("(%d)conn: %s"%( fd, address))
                    connections[fd] = client
                    clientIn[fd] = []
                    clientOut[fd] = ["Welcome to my server\n"]
                    epoll.register(fd, select.EPOLLOUT)

                elif event & select.EPOLLIN:
                    if fileno in cmdOut:
                        stdout, pfileno, _ = cmdOut[fileno]
                        if pfileno in connections:
                            output = stdout.read()
                            clientOut[pfileno].append(output)
                            epoll.modify(pfileno, select.EPOLLOUT)
                        else:
                           destroy(pfileno)
                    else:
                        try:
                            data = connections[fileno].recv(1024)
                        except:
                            data = None
                        if data :
                            clientIn[fileno].append(data)
                            epoll.modify(fileno, select.EPOLLOUT)
                            dealCmd(data, fileno)
                        else:##客户端主动断开
                            destroy(fileno)

                elif event & select.EPOLLOUT:
                    if fileno in clientOut :
                        outData = "".join(clientOut[fileno])
                        try:
                            byteswritten = connections[fileno].send(outData)
                        except:
                            destroy(fileno)
                        leftData = outData[byteswritten:]
                        if leftData:
                            clientOut[fileno] = [leftData]
                        else:
                            clientOut[fileno] = []
                            if fileno in destroySet:
                                destroy(fileno)
                    try:
                        epoll.modify(fileno, select.EPOLLIN)
                    except:
                        destroy(fileno)

                elif event & select.EPOLLHUP:
                    if fileno in cmdOut:
                        _, pfileno, _ = cmdOut[fileno]
                        destroy(pfileno)
                    else:
                        destroy(fileno)
    except KeyboardInterrupt:
        log("finish")
    except:
        content = str(traceback.format_exc())
        log("Except____", content)
        sendPopoTo("ginew", content)
    finally:
        epoll.unregister(serverSocket.fileno())
        epoll.close()
        serverSocket.close()

if __name__ == "__main__":
    main()
