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

class AZDownloader():
    def __init__(self):
        pass

    def download():
        pass

azd = AZDownloader()
webview.create_window("Hello PyWebView", "https://www.python.org", jsapi=azd)
#production - start with SSL?
webview.start()

