from multiprocessing import Pool
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
import tomllib
import os
import sys


def download(args:tuple)-> None:
    '''
      Function to download the APKs from AndroZoo and 
      write to disk to be moved to the pipeline. 

      :param args a tuple of the hash, key and base directory
    '''

    try:
        apk_hash, api_key, basedir = args
        _apk = urlopen(f"https://androzoo.uni.lu/api/download?apikey=${api_key}&sha256=${apk_hash}")
        with open(basedir + "/" + apk_hash, 'w') as f:
            f.write(_apk.read())

    except (URLError, HTTPError, Exception) as e:
        print(e)

def _read_config():
    '''
        Read the Config file
    '''
    with open("azkey.toml", "rb") as f:
        return tomllib.load(f)

def main():
    """
    
    """
    init_len_files = 0
    try:
        if not os.path.exists("azkey.ini"):
            raise Exception("An azkey.ini file is required.")

        config = _read_config()
        api_key = config['key']
        basedir = config['basedir']
        hash_file = config['input_file']

        if not os.path.exists(basedir): 
            os.mkdir(basedir)

        with open(hash_file, "r") as fh:
            apks = fh.readlines()
            init_len_files = len(apks)

    except Exception as e:
        print(e)

    args_list = [(apk, api_key, basedir) for apk in apks]
    with Pool(5) as p:
        p.map(download, args_list)

    final_apks = [name for name in os.listdir('.') if os.path.isfile(name) and name.endswith('.apk')]
    final_list = len(final_apks)

    if init_len_files == final_list:
        print("All files downloaded")
        sys.exit()
    else:
        print("Retrying failed files")
        re_run = [apk for apk in apks if apk not in final_apks]
        args_list = [(apk, api_key, basedir) for apk in re_run]
        with Pool(5) as p:
            p.map(download, args_list)

        final_apks = [name for name in os.listdir(basedir) if os.path.isfile(name) and name.endswith('.apk')]
        final_list = len(final_apks)
        if init_len_files == final_list:
            print("All files downloaded at second try")
            sys.exit()
        else: 
            fail = ";".join([name for name in os.listdir(basedir) if os.path.isfile(name) and name.endswith('.apk')])
            print(f"Sorry, these ones did not download {fail}")


if __name__ == '__main__':
    main()

