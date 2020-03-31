#!/usr/bin/python3.6
#author: Simon Preissner

"""
This module provides helper functions.
"""

import os
import sys
import re
import json
#from tqdm import tqdm
from time import time
from torch import nn
import torch
from torch import functional as F


def loop_input(rtype=str, default=None, msg=""):
    """
    Wrapper function for command-line input that specifies an input type
    and a default value. Input types can be string, int, float, or bool,
    or "file", so that only existing files will pass the input.
    :param rtype: type of the input. one of str, int, float, bool, "file"
    :type rtype: type
    :param default: value to be returned if the input is empty
    :param msg: message that is printed as prompt
    :type msg: str
    :return: value of the specified type
    """
    while True:
        try:
            s = input(msg+f" (default: {default}): ")
            if rtype == bool and len(s) > 0:
                if s=="True":
                    return True
                elif s=="False":
                    return False
                else:
                    print("Input needs to be convertable to",rtype,"-- try again.")
                    continue
            if rtype == "filepath":
                s = default if len(s) == 0 else s
                try:
                    f = open(s, "r")
                    f.close()
                    return s
                except FileNotFoundError as e:
                    print("File",s,"not found -- try again.")
                    continue
            else:
                return rtype(s) if len(s) > 0 else default
        except ValueError:
            print("Input needs to be convertable to",rtype,"-- try again.")
            continue

def flatten_context(context, siyana_wants_a_oneliner=False):
        """
        return the context as a single string,
        :param context: list[ list[ str, list[str] ] ]
        :return: string containing the whole context
        """

        if siyana_wants_a_oneliner:  # This is for you, Siyana!
            return " ".join([p[0] + " " + " ".join(["".join(s) for s in p[1:]]) for p in context])

        final = ""
        for para in context:
            for sent in para:
                if type(sent) == list:
                    final += "".join(sent) + " "
                else:
                    final += sent + " "
        final = final.rstrip()
        return final

class ConfigReader():
    """
    Basic container and management of parameter configurations.
    Read a config file (typically ending with .cfg), use this as container for
    the parameters during runtime, and change/write parameters.

    CONFIGURATION FILE SYNTAX
    - one parameter per line, containing a name and a value
        - name and value are separated by at least one white space or tab
        - names should contain alphanumeric symbols and '_' (no '-', please!)
    - list-like values are allowed (use Python list syntax)
        - strings within value lists don't need to be quoted
        - value lists either with or without quotation (no ["foo", 3, "bar"] )
        - mixed lists will exclude non-quoted elements
    - multi-word expressions are marked with single or double quotation marks
    - TODO strings containing quotation marks are not tested yet. Be careful!
    - lines starting with '#' are ignored
    - no in-line comments!
    - config files should have the extension 'cfg' (to indicate their purpose)
    """

    def __init__(self, filepath):
        self.filepath = filepath
        self.params = self.read_config()

    def __repr__(self):
        """
        returns tab-separated key-value pairs (one pair per line)
        """
        return "\n".join([str(k)+"\t"+str(v) for k,v in self.params.items()])

    def __call__(self, *paramnames):
        """
        Returns a single value or a list of values corresponding to the
        provided parameter name(s). Returns the whole config in form of a
        dictionary if no parameter names are specified.
        """
        if not paramnames: # return the whole config
            return self.params
        else: # return specified values
            values = [self.params[n] if n in self.params else None for n in paramnames]
            return values[0] if len(values) == 1 else values

    def read_config(self):
        """
        Reads the ConfigReader's assigned file (attribute: 'filename') and parses
        the contents into a dictionary.
        - ignores empty lines and lines starting with '#'
        - takes the first continuous string as parameter key (or: parameter name)
        - parses all subsequent strings (splits at whitespaces) as values
        - tries to convert each value to float, int, and bool. Else: string.
        - parses strings that look like Python lists to lists
        :return: dict[str:obj]
        """
        cfg = {}
        with open(self.filepath, "r") as f:
            lines = f.readlines()

        for line in lines:
            line = line.rstrip()
            if not line: # ignore empty lines
                continue
            elif line.startswith('#'): # ignore comment lines
                continue

            words = line.split()
            paramname = words.pop(0)
            if not words: # no value specified
                print(f"WARNING: no value specified for parameter {paramname}.")
                paramvalue = None
            elif words[0].startswith("["): # detects a list of values
                paramvalue = self.listparse(" ".join(words))
            elif words[0].startswith('"') or words[0].startswith('\''): # detects a multi-word string
                paramvalue = self.stringparse(words)
            else:
                """ only single values are valid! """
                if len(words) > 1:
                    #TODO make this proper error handling (= throw an error)
                    print(f"ERROR while parsing {self.filepath} --",
                          f"too many values in line '{line}'.")
                    sys.exit()
                else:
                    """ parse the single value """
                    paramvalue = self.numberparse(words[0]) # 'words' is still a list
                    paramvalue = self.boolparse(paramvalue)

            cfg[paramname] = paramvalue # adds the parameter to the config

        self.config = cfg
        return self.config

    @classmethod
    def listparse(cls, liststring):
        """
        Parses a string that looks like a Python list (square brackets, comma
        separated, ...). A list of strings can make use of quotation marks, but
        doesn't need to. List-like strings that contain some quoted and some
        unquoted elements will be parsed to only return the quoted elements.
        Elements parsed from an unquoted list will be converted to numbers/bools
        if possible.
        Examples:
            [this, is, a, valid, list] --> ['this', 'is', 'a', 'valid', 'list']
            ["this", "one", "too"]     --> ['this', 'one', 'too']
            ['single', 'quotes', 'are', 'valid'] --> ['single', 'quotes', 'are', 'valid']
            ["mixing", 42, is, 'bad']  --> ['mixing', 'bad']
            ["54", "74", "90", "2014"] --> ['54', '74', '90', '2014']
            [54, 74, 90, 2014]         --> [54, 74, 90, 2014]
            [True, 1337, False, 666]   --> [True, 1337, False, 666]
            [True, 1337, "bla", False, 666] --> ['bla']
        """
        re_quoted = re.compile('["\'](.+?)["\'][,\]]')
        elements = re.findall(re_quoted, liststring)
        if elements:
            return elements

        re_unquoted = re.compile('[\[\s]*(.+?)[,\]]')
        elements = re.findall(re_unquoted, liststring)
        if elements:
            result = []
            for e in elements:
                e = cls.numberparse(e)  # convert to number if possible
                e = cls.boolparse(e)  # convert to bool if possible
                result.append(e)
            return result

    @staticmethod
    def stringparse(words):
        words[0] = words[0][1:] # delete opening quotation marks
        words[-1] = words[-1][:-1] #delete closing quotation marks
        return " ".join(words)

    @staticmethod
    def numberparse(string):
        """
        Tries to convert 'string' to a float or even int.
        Returns int/float if successful, or else the input string.
        """
        try:
            floaty = float(string)
            if int(floaty) == floaty:
                return int(floaty)
            else:
                return floaty
        except ValueError:
            return string

    @staticmethod
    def boolparse(string):
        if string == "True" or string == "False":
            return bool(string)
        else:
            return string

    def get_param_names(self):
        """
        returns a list of parameter names
        """
        return [key for key in self.params.keys()]

    def set(self, paramname, value):
        self.params.update({paramname:value})

class Timer():
    # TODO docstring
    def __init__(self):
        self.T0 = time()
        self.t0 = time()
        self.times = {}
        self.steps = []
        self.period_name = ""

    def __call__(self, periodname):
        span = time() - self.t0
        self.t0 = time()
        self.steps.append(periodname)
        self.times.update({periodname: span})
        return span

    def __repr__(self, *args):
        steps = [s for s in args if s in self.steps] if args else self.steps
        return "\n".join([str(round(self.times[k], 5)) + "   " + k for k in steps])

    def total(self):
        span = time() - self.T0
        self.steps.append("total")
        self.times.update({"total": span})
        return span

class HotPotDataHandler():
    """
    This class provides an interface to the HotPotQA dataset.
    It implements functions that tailor the required information to each module.
    #TODO docstring
    """

    def __init__(self, filename="./data/hotpot_train_v1.1.json"):
        self.filename = os.path.abspath(filename)
        with open(self.filename, "r") as f:
            self.data = json.load(f)

    def __repr__(self):
        header = str(len(self.data))+" items in data; keys:\n"
        content = "\n".join([str(k) for k in self.data[0].keys()]).rstrip()
        return header+content

    def data_for_paragraph_selector(self):
        """
        #TODO docstring
        Returns a list of tuples (question_id, supporting_facts, query, paragraphs),
        where supporting_facts is a list of strings,
        query is a string,
        paragraphs is a 10-element list where
            the first element is a string
            the second element is a list of sentences (i.e., a list of strings)
        """
        result = []
        for point in self.data:
            supp_facts = set([fact[0] for fact in point["supporting_facts"]])
            result.append(tuple((point["_id"],
                                 supp_facts,
                                 point["question"],
                                 point["context"])))
        return result

class Linear(nn.Module):
    '''
    Linear class for the BiDAF network.
    Source: https://github.com/galsang/BiDAF-pytorch/blob/master/utils/nn.py
    '''
    def __init__(self, in_features, out_features, dropout=0.0):
        super(Linear, self).__init__()

        self.linear = nn.Linear(in_features=in_features, out_features=out_features)
        if dropout > 0:
            self.dropout = nn.Dropout(p=dropout)
        self.reset_params()

    def reset_params(self):
        nn.init.kaiming_normal_(self.linear.weight)
        nn.init.constant_(self.linear.bias, 0)

    def forward(self, x):
        if hasattr(self, 'dropout'):
            x = self.dropout(x)
        x = self.linear(x)
        return x

class BiDAFNet(torch.nn.Module):
    """
    TODO: write docstring

    BiDAF paper: arxiv.org/pdf/1611.01603.pdf
    There's a link to the code, but that uses TensorFlow

    We adapted this implementation of the BiDAF
    Attention Layer: https://github.com/galsang/BiDAF-pytorch
    """

    def __init__(self, hidden_size=768, output_size=300):
        super(BiDAFNet, self).__init__()

        self.att_weight_c = Linear(hidden_size, 1)
        self.att_weight_q = Linear(hidden_size, 1)
        self.att_weight_cq = Linear(hidden_size, 1)

        self.reduction_layer = Linear(hidden_size * 4, output_size)

    def forward(self, emb1, emb2, batch=1):
        """
        perform bidaf and return the updated emb2.
        using 'q' and 'c' instead of 'emb1' and 'emb2' for readability
        :param emb2: (batch, c_len, hidden_size)
        :param emb1: (batch, q_len, hidden_size)
        :return: (batch, c_len, output_size)
        """
        c_len = emb2.size(1)
        q_len = emb1.size(1)

        cq = []
        for i in range(q_len):
            qi = emb1.select(1, i).unsqueeze(1)  # (batch, 1, hidden_size)
            ci = self.att_weight_cq(emb2 * qi).squeeze(-1)  # (batch, c_len, 1)
            cq.append(ci)
        cq = torch.stack(cq, dim=-1)  # (batch, c_len, q_len)

        # (batch, c_len, q_len)
        s = self.att_weight_c(emb2).expand(-1, -1, q_len) + \
            self.att_weight_q(emb1).permute(0, 2, 1).expand(-1, c_len, -1) + \
            cq

        a = F.softmax(s, dim=2)  # (batch, c_len, q_len)

        # (batch, c_len, q_len) * (batch, q_len, hidden_size) -> (batch, c_len, hidden_size)
        c2q_att = torch.bmm(a, emb1)

        b = F.softmax(torch.max(s, dim=2)[0], dim=1).unsqueeze(1)  # (batch, 1, c_len)

        # (batch, 1, c_len) * (batch, c_len, hidden_size) -> (batch, hidden_size)
        q2c_att = torch.bmm(b, emb2).squeeze(1)

        # (batch, c_len, hidden_size) (tiled)
        q2c_att = q2c_att.unsqueeze(1).expand(-1, c_len, -1)

        # (batch, c_len, hidden_size * 4)
        x = torch.cat([emb2, c2q_att, emb2 * c2q_att, emb2 * q2c_att], dim=-1)
        x = self.reduction_layer(x)  # (batch, c_len, output_size)
        return x







