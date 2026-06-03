"""
    UI for the downloader. 
    Uses pywebview in serverless mode. 
"""
from multiprocessing import Pool
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
import tomllib
import os
import sys

import pywebview

