#!/usr/bin/env python
# @lint-avoid-python-3-compatibility-imports
#
# ext4slower  Trace slow ext4 operations.
#             For Linux, uses BCC, eBPF.
#
# USAGE: ext4slower [-h] [-j] [-p PID] [min_ms]
#
# This script traces common ext4 file operations: reads, writes, opens, and
# syncs. It measures the time spent in these operations, and prints details
# for each that exceeded a threshold.
#
# WARNING: This adds low-overhead instrumentation to these ext4 operations,
# including reads and writes from the file system cache. Such reads and writes
# can be very frequent (depending on the workload; eg, 1M/sec), at which
# point the overhead of this tool (even if it prints no "slower" events) can
# begin to become significant.
#
# By default, a minimum millisecond threshold of 10 is used.
#
# Copyright 2016 Netflix, Inc.
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 11-Feb-2016   Brendan Gregg   Created this.
# 15-Oct-2016   Dina Goldshtein -p to filter by process ID.
# 13-Jun-2018   Joe Yin modify generic_file_read_iter to ext4_file_read_iter.

from __future__ import print_function
from bcc import BPF
import argparse
from time import strftime

# symbols
kallsyms = "/proc/kallsyms"

# arguments
examples = """examples:
    ./ext4slower             # trace operations slower than 10 ms (default)
    ./ext4slower 1           # trace operations slower than 1 ms
    ./ext4slower -j 1        # ... 1 ms, parsable output (csv)
    ./ext4slower 0           # trace all operations (warning: verbose)
    ./ext4slower -p 185      # trace PID 185 only
"""
parser = argparse.ArgumentParser(
    description="Trace common ext4 file operations slower than a threshold",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-j", "--csv", action="store_true",
    help="just print fields: comma-separated values")
parser.add_argument("-p", "--pid",
    help="trace this PID only")
parser.add_argument("min_ms", nargs="?", default='10',
    help="minimum I/O duration to trace, in ms (default 10)")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)
args = parser.parse_args()
min_ms = int(args.min_ms)
pid = args.pid
csv = args.csv
debug = 0

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/sched.h>
#include <linux/dcache.h>

struct val_t {
    u64 ts;
    u64 offset;
    struct file *fp;
};

struct data_t {
    // XXX: switch some to u32's when supported
    u64 ts_us;
    char type;  // R (read), W (write), O (open), S (fsync)
    u32 size;
    u64 offset;
    u64 delta_us;
    u32 pid;
    char task[TASK_COMM_LEN];
    char file[DNAME_INLINE_LEN];
};

BPF_HASH(entryinfo, u64, struct val_t);
BPF_PERF_OUTPUT(events);

//
// Store timestamp and size on entry
//

// The current ext4 (Linux 4.5) uses generic_file_read_iter(), instead of it's
// own function, for reads. So we need to trace that and then filter on ext4,
// which I do by checking file->f_op.
// The new Linux version (since form 4.10) uses ext4_file_read_iter(), And if the 'CONFIG_FS_DAX' 
// is not set, then ext4_file_read_iter() will call generic_file_read_iter(), else it will call
// ext4_dax_read_iter(), and trace generic_file_read_iter() will fail.
int trace_read_entry(struct pt_regs *ctx, struct kiocb *iocb)
{
    u64 id =  bpf_get_current_pid_tgid();
    u32 pid = id >> 32; // PID is higher part

    if (FILTER_PID)
        return 0;

    // ext4 filter on file->f_op == ext4_file_operations
    struct file *fp = iocb->ki_filp;
    if ((u64)fp->f_op != EXT4_FILE_OPERATIONS)
        return 0;

    // store filep and timestamp by id
    struct val_t val = {};
    val.ts = bpf_ktime_get_ns();
    val.fp = fp;
    val.offset = iocb->ki_pos;
    if (val.fp)
        entryinfo.update(&id, &val);

    return 0;
}

// ext4_file_write_iter():
int trace_write_entry(struct pt_regs *ctx, struct kiocb *iocb)
{
    u64 id = bpf_get_current_pid_tgid();
    u32 pid = id >> 32; // PID is higher part

    if (FILTER_PID)
        return 0;

    // store filep and timestamp by id
    struct val_t val = {};
    val.ts = bpf_ktime_get_ns();
    val.fp = iocb->ki_filp;
    val.offset = iocb->ki_pos;
    if (val.fp)
        entryinfo.update(&id, &val);

    return 0;
}

// ext4_file_open():
int trace_open_entry(struct pt_regs *ctx, struct inode *inode,
    struct file *file)
{
    u64 id = bpf_get_current_pid_tgid();
    u32 pid = id >> 32; // PID is higher part

    if (FILTER_PID)
        return 0;

    // store filep and timestamp by id
    struct val_t val = {};
    val.ts = bpf_ktime_get_ns();
    val.fp = file;
    val.offset = 0;
    if (val.fp)
        entryinfo.update(&id, &val);

    return 0;
}

// ext4_sync_file():
int trace_fsync_entry(struct pt_regs *ctx, struct file *file)
{
    u64 id = bpf_get_current_pid_tgid();
    u32 pid = id >> 32; // PID is higher part

    if (FILTER_PID)
        return 0;

    // store filep and timestamp by id
    struct val_t val = {};
    val.ts = bpf_ktime_get_ns();
    val.fp = file;
    val.offset = 0;
    if (val.fp)
        entryinfo.update(&id, &val);

    return 0;
}

//
// Output
//

static int trace_return(struct pt_regs *ctx, char type)
{
    struct val_t *valp;
    u64 id = bpf_get_current_pid_tgid();
    u32 pid = id >> 32; // PID is higher part

    valp = entryinfo.lookup(&id);
    if (valp == 0) {
        // missed tracing issue or filtered
        return 0;
    }

    // calculate delta
    u64 ts = bpf_ktime_get_ns();
    u64 delta_us = (ts - valp->ts) / 1000;
    entryinfo.delete(&id);
    if (FILTER_US)
        return 0;

    // populate output struct
    struct data_t data = {};
    data.type = type;
    data.size = PT_REGS_RC(ctx);
    data.delta_us = delta_us;
    data.pid = pid;
    data.ts_us = ts / 1000;
    data.offset = valp->offset;
    bpf_get_current_comm(&data.task, sizeof(data.task));

    // workaround (rewriter should handle file to d_name in one step):
    struct dentry *de = NULL;
    struct qstr qs = {};
    de = valp->fp->f_path.dentry;
    qs = de->d_name;
    if (qs.len == 0)
        return 0;
    bpf_probe_read_kernel(&data.file, sizeof(data.file), (void *)qs.name);

    // output
    events.perf_submit(ctx, &data, sizeof(data));

    return 0;
}

int trace_read_return(struct pt_regs *ctx)
{
    return trace_return(ctx, 'R');
}

int trace_write_return(struct pt_regs *ctx)
{
    return trace_return(ctx, 'W');
}

int trace_open_return(struct pt_regs *ctx)
{
    return trace_return(ctx, 'O');
}

int trace_fsync_return(struct pt_regs *ctx)
{
    return trace_return(ctx, 'S');
}

"""

# code replacements
with open(kallsyms) as syms:
    ops = ''
    for line in syms:
        (addr, size, name) = line.rstrip().split(" ", 2)
        name = name.split("\t")[0]
        if name == "ext4_file_operations":
            ops = "0x" + addr
            break
    if ops == '':
        print("ERROR: no ext4_file_operations in /proc/kallsyms. Exiting.")
        print("HINT: the kernel should be built with CONFIG_KALLSYMS_ALL.")
        exit()
    bpf_text = bpf_text.replace('EXT4_FILE_OPERATIONS', ops)
if min_ms == 0:
    bpf_text = bpf_text.replace('FILTER_US', '0')
else:
    bpf_text = bpf_text.replace('FILTER_US',
        'delta_us <= %s' % str(min_ms * 1000))
if args.pid:
    bpf_text = bpf_text.replace('FILTER_PID', 'pid != %s' % pid)
else:
    bpf_text = bpf_text.replace('FILTER_PID', '0')
if debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

# process event
def print_event(cpu, data, size):
    event = b["events"].event(data)
    type = event.type.decode('utf-8', 'replace')

    if (csv):
        print("%d,%s,%d,%s,%d,%d,%d,%s" % (
            event.ts_us, event.task.decode('utf-8', 'replace'), event.pid,
            type, event.size, event.offset, event.delta_us,
            event.file.decode('utf-8', 'replace')))
        return
    print("%-8s %-14.14s %-6s %1s %-7s %-8d %7.2f %s" % (strftime("%H:%M:%S"),
        event.task.decode('utf-8', 'replace'), event.pid, type, event.size,
        event.offset / 1024, float(event.delta_us) / 1000,
        event.file.decode('utf-8', 'replace')))

# initialize BPF
b = BPF(text=bpf_text)

# Common file functions. See earlier comment about generic_file_read_iter().
if BPF.get_kprobe_functions(b'ext4_file_read_iter'):
    b.attach_kprobe(event="ext4_file_read_iter", fn_name="trace_read_entry")
else:
    b.attach_kprobe(event="generic_file_read_iter", fn_name="trace_read_entry")
b.attach_kprobe(event="ext4_file_write_iter", fn_name="trace_write_entry")
b.attach_kprobe(event="ext4_file_open", fn_name="trace_open_entry")
b.attach_kprobe(event="ext4_sync_file", fn_name="trace_fsync_entry")
if BPF.get_kprobe_functions(b'ext4_file_read_iter'):
    b.attach_kretprobe(event="ext4_file_read_iter", fn_name="trace_read_return")
else:
    b.attach_kretprobe(event="generic_file_read_iter", fn_name="trace_read_return")
b.attach_kretprobe(event="ext4_file_write_iter", fn_name="trace_write_return")
b.attach_kretprobe(event="ext4_file_open", fn_name="trace_open_return")
b.attach_kretprobe(event="ext4_sync_file", fn_name="trace_fsync_return")

# header
if (csv):
    print("ENDTIME_us,TASK,PID,TYPE,BYTES,OFFSET_b,LATENCY_us,FILE")
else:
    if min_ms == 0:
        print("Tracing ext4 operations")
    else:
        print("Tracing ext4 operations slower than %d ms" % min_ms)
    print("%-8s %-14s %-6s %1s %-7s %-8s %7s %s" % ("TIME", "COMM", "PID", "T",
        "BYTES", "OFF_KB", "LAT(ms)", "FILENAME"))

# read events
b["events"].open_perf_buffer(print_event, page_cnt=64)
while 1:
    try:
        b.perf_buffer_poll()
    except KeyboardInterrupt:
        exit()
