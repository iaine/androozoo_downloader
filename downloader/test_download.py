import pytest  
from download import main, _read_config

def test_read_config(): 
    conf = _read_config()
    assert conf != None

def test_read_config_fails():  
    with pytest.raises(Exception):  
        pass