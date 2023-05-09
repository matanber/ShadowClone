import datetime
from lithops import FunctionExecutor
from lithops import Storage
import argparse
import os.path
from invoke import run
import uuid
import sys
import config
import tempfile
from multiprocessing.pool import ThreadPool
from hasher import perform_hashing
import pickledb
import boto3


def printerr(msg):
    sys.stderr.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3] +" " + msg + "\n")


def splitfile(infile, SPLIT_NUM):
    # from https://stackoverflow.com/questions/16289859/splitting-large-text-file-into-smaller-text-files-by-line-numbers-using-python
    lines_per_file = int(SPLIT_NUM)

    smallfile = None
    chunks = []
    with open(infile) as bigfile:
        tempdir = tempfile.gettempdir()
        for lineno, line in enumerate(bigfile):
            if lineno % lines_per_file == 0:
                if smallfile:
                    smallfile.close()
                # small_filename = '/tmp/small_file_{}.txt'.format(lineno + lines_per_file)
                small_filename = os.path.join(tempdir, 'small_file_{}.txt'.format(lineno + lines_per_file))
                chunks.append(small_filename)
                smallfile = open(small_filename, "w")
            smallfile.write(line)
        if smallfile:
            smallfile.close()
    return chunks


def upload_to_bucket(chunk, keep_extension=False):
    chunk_hash = perform_hashing(chunk)

    if db.exists(chunk_hash):
        # same file already exists in bucket
        printerr("[INFO] This file is already uploaded to bucket. Skipping upload.")
        return db.get(chunk_hash)

    bucket_name = config.STORAGE_BUCKET

    filename, ext = os.path.splitext(chunk)
    f = open(chunk,'r')
    contents = f.read()
    if keep_extension:
        upload_key = str(uuid.uuid4()) + ext
    else:
        upload_key = str(uuid.uuid4())

    try:
        storage.put_object(bucket=bucket_name, key=upload_key, body=contents)
        db.set(chunk_hash, bucket_name+'/'+upload_key)
        printerr("[INFO] File uploaded successfully: "+ bucket_name + "/" + upload_key)
    except Exception as e:
        printerr("[ERROR] Error occured while accessing the storage bucket. Did you update the config.py file?")
        # exit()
        pass    
    return bucket_name+'/'+upload_key


def delete_bucket_files(fileslist):
    printerr("[INFO] Cleaning up")
    # storage = Storage()
    keys = []
    bucket_name = fileslist[0].split('/')[0]
    for f in fileslist:
        keys.append(f.split('/')[1])
    
    # delete all at once
    try:
        storage.delete_objects(bucket=bucket_name, key_list=keys)
    except Exception as e:
        raise e
    return

# This function runs in cloud
def execute_command(obj, command, nosplit):
    if not command: #hopefully we never go into this
        return "LITHOPS ERROR! Command was not received by the cloud function! Re-run the same command."

    from invoke import run
    data = obj.data_stream.read()

    with open('/tmp/infile','w') as infile:
        for line in data.splitlines():
            infile.write(line.decode("UTF-8")+'\n')

    command = command.replace('{INPUT}', '/tmp/infile')
    command = command.replace('{OUTPUT}', '/tmp/outfile')

    #go into this only when a nosplit file is provided
    if nosplit: 
        # print(nosplit)\

        s3 = boto3.client('s3')
        bucket_name = nosplit.split('/')[0]
        # print(bucket_name)
        object_name = nosplit.split('/')[1]
        file_name = '/tmp/' + object_name

        try:
            s3.download_file(bucket_name, object_name, file_name)
        except:
            pass
        command = command.replace('{NOSPLIT}',file_name)

    try:
        results = run(command, warn=True)
    except Exception as e:
        print("Error in running the command:"+ command)
        return str(e)
    return results.stdout


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input', dest='input', required=True, help="File that you want to distribute")
    parser.add_argument('-s', '--split', dest='splitnum', required=False, help="Number of lines per chunk of file")
    parser.add_argument('-o', '--output', dest='output', required=False, help="Write output to a file")
    parser.add_argument('-c', '--command', dest='command', required=True, help="Command to execute") 
    parser.add_argument('--no-split', dest='nosplit', required=False, help="File to be used without splitting")
    args = parser.parse_args()

    runtime = config.LITHOPS_RUNTIME
    bucket_name = config.STORAGE_BUCKET
    infile = args.input
    command = args.command
    storage = Storage()
    # initiate pickle db
    db = pickledb.load(config.PICKLE_DB, False) # set auto-dump to false

    if args.splitnum:
        try:
            SPLIT_NUM = int(args.splitnum)
        except:
            SPLIT_NUM = 1000
    else:
        SPLIT_NUM = 1000

    if args.nosplit:
        nosplit_file = args.nosplit
        if os.path.exists(nosplit_file):
            printerr("[INFO] Uploading file to bucket without splitting")
            nosplit_s3 = upload_to_bucket(nosplit_file, True)
            # print(nosplit_s3)
        else:
            printerr("[ERROR] --nosplit: File not found")
            exit(0)
    else:
        nosplit_s3 = None


    if os.path.exists(infile):        
        printerr("[INFO] Splitting input file into chunks of "+ str(SPLIT_NUM) +" lines")
        chunks = splitfile(infile, SPLIT_NUM)
        if len(chunks) == 0: #empty file
            pool = ThreadPool(processes=1)
        elif len(chunks) < 100:
            pool = ThreadPool(processes=len(chunks)) 
        else:
            pool = ThreadPool(processes=100) # max 100 threads

        printerr("[INFO] Uploading chunks to storage")
        filekeys = pool.map(upload_to_bucket, chunks)
        db.dump()  # save the db to file
        try:
            fexec = FunctionExecutor(runtime=runtime) # change runtime memory if required
            fexec.map(execute_command, filekeys, extra_args=(command, nosplit_s3,))
            output = fexec.get_result()
        except Exception as e:
            printerr("[ERROR] Could not execute the runtime.")
            print(e)
            sys.exit(1)
    else:
        printerr("[ERROR] Input file not found")
        sys.exit(1)

    # print(output)
    for line in output:
        if len(line):
            if args.output:
                try:
                    with open(args.output,'a') as outfile:
                        outfile.write(line)
                except:
                    pass
            print(line)

    # delete input files from bucket
    # delete_bucket_files(filekeys)


