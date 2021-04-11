import numpy as np
import sympy
import warnings
import torch
from torch.utils import data
import math
from nesymres.utils import load_metadata_hdf5, load_eq
from sympy.core.rules import Transform
from sympy import sympify, Float, Symbol
from multiprocessing import Manager
from numpy import (
    log,
    cosh,
    sinh,
    exp,
    cos,
    tanh,
    sqrt,
    sin,
    tan,
    arctan,
    nan,
    pi,
    e,
    arcsin,
    arccos,
)

import types
from typing import List
import random
from torch.distributions.uniform import Uniform
from ..dclasses import DataModuleParams, NNEquation
from ..dataset.generator import Generator, UnknownSymPyOperator
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
from nesymres.dclasses import Params, Dataset, Equation, DatasetParams, ConstantsOptions
from functools import partial
from ordered_set import OrderedSet
from pathlib import Path
import hydra


class NesymresDataset(data.Dataset):
    def __init__(
        self,
        data_path: Path,
        cfg,
        mode: str
    ):  
        #m = Manager()
        #self.eqs = m.dict({i:eq for i, eq in enumerate(data.eqs)})
        other = load_metadata_hdf5(hydra.utils.to_absolute_path(data_path))
        self.len = other.total_number_of_eqs
        self.eqs_per_hdf = other.eqs_per_hdf
        self.data_params = data_params
        self.word2id = other.word2id
        self.data_path = data_path
        self.mode = mode
        self.cfg = cfg

    def __getitem__(self, index):
        
        eq = load_eq(self.data_path, index, self.eqs_per_hdf)
        code = types.FunctionType(eq.code, globals=globals(), name="f")
        
        consts = sample_constants(eq, self.constant_options)

        if self.data_params.predict_c:
            eq_string = eq.expr.format(**consts)
        else:
            eq_string = eq.expr.format(**initial_consts)

        eq_sympy_infix = constants_to_placeholder(eq_string)
        try:
            eq_sympy_prefix = Generator.sympy_to_prefix(eq_sympy_infix)
        except UnknownSymPyOperator as e:
            print(e)
            return Equation(code=code,expr=[],coeff_dict=consts,variables=eq.variables,support=eq.support, valid=False)


        try:
            t = tokenize(eq_sympy_prefix,self.word2id)
            curr = Equation(code=code,expr=eq_sympy_infix,coeff_dict=consts,variables=eq.variables,support=eq.support, tokenized=t, valid=True)
        except:
            t = []
            curr = Equation(code=code,expr=eq_sympy_infix,coeff_dict=consts,variables=eq.variables,support=eq.support, valid=False)

        return curr

    def __len__(self):
        return self.len

def custom_collate_fn(eqs: List[Equation], cfg: DatasetParams = None) -> NNEquation:
    filtered_eqs = [eq for eq in eqs if eq.valid]
    res, tokens = evaluate_and_wrap(filtered_eqs, cfg)
    return res, tokens, [eq.expr for eq in filtered_eqs]


def constants_to_placeholder(s,symbol="c"):
    try:
        sympy_expr = sympify(s)  # self.infix_to_sympy("(" + s + ")")
        sympy_expr = sympy_expr.xreplace(
            Transform(
                lambda x: Symbol(symbol, real=True, nonzero=True),
                lambda x: isinstance(x, Float),
            )
        )
    except:
        breakpoint()
    return sympy_expr

def tokenize(prefix_expr:list, word2id:dict) -> list:
    tokenized_expr = []
    tokenized_expr.append(word2id["S"])
    for i in prefix_expr:
        tokenized_expr.append(word2id[i])
    tokenized_expr.append(word2id["F"])
    return tokenized_expr

def de_tokenize(tokenized_expr, id2word:dict):
    prefix_expr = []
    for i in tokenized_expr:
        if "F" == id2word[i]:
            break
        else:
            prefix_expr.append(id2word[i])
    return prefix_expr

def tokens_padding(tokens):
    max_len = max([len(y) for y in tokens])
    p_tokens = torch.zeros(len(tokens), max_len)
    for i, y in enumerate(tokens):
        y = torch.tensor(y).long()
        p_tokens[i, :] = torch.cat([y, torch.zeros(max_len - y.shape[0]).long()])
    return p_tokens

def number_of_support_points(p, type_of_sampling_points):
    if type_of_sampling_points == "constant":
        curr_p = p
    elif type_of_sampling_points == "logarithm":
        curr_p = int(10 ** Uniform(1, math.log10(p)).sample())
    else:
        raise NameError
    return curr_p

def sample_support(eq, curr_p, cfg: DatasetParams):
    sym = []
    if not eq.support:
        distribution = cfg.distribution_support(cfg.fun_support[0],cfg.fun_support[1])
    else:
        raise NotImplementedError
    
    for sy in cfg.total_variables:
        if sy in eq.variables:
            curr = distribution.sample([int(curr_p)])
        else:
            curr = torch.zeros(int(curr_p))
        sym.append(curr)
    return torch.stack(sym)

def sample_constants(eq, curr_p, cfg:DatasetParams):
    consts = []
    #eq_c = set(eq.coeff_dict.values())
    for c in cfg.total_coefficients:
        if c[:2] == "cm":
            if c in eq.coeff_dict:
                curr = torch.ones([int(curr_p)]) * eq.coeff_dict[c]
            else:
                curr = torch.ones([int(curr_p)])
        elif c[:2] == "ca":
            if c in eq.coeff_dict:
                curr = torch.ones([int(curr_p)]) * eq.coeff_dict[c]
            else:
                curr = torch.zeros([int(curr_p)])
        consts.append(curr)
    return torch.stack(consts)

def evaluate_and_wrap(eqs: List[Equation], cfg: DatasetParams):
    vals = []
    cond0 = []
    tokens_eqs = [eq.tokenized for eq in eqs]
    tokens_eqs = tokens_padding(tokens_eqs)
    curr_p = number_of_support_points(cfg.max_number_of_points, cfg.type_of_sampling_points)
    for eq in eqs:
        support = sample_support(eq, curr_p, cfg)
        consts = sample_constants(eq,curr_p,cfg)
        input_lambdi = torch.cat([support, consts], axis=0)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                aaaa = eq.code(*input_lambdi)
                if type(aaaa) == torch.Tensor and aaaa.dtype == torch.float32:
                    vals.append(
                        torch.cat(
                            [support, torch.unsqueeze(aaaa, axis=0)], axis=0
                        ).unsqueeze(0)
                    )
                    cond0.append(True)
                else:
                    cond0.append(False)
        except NameError as e:
            # print(e)
            cond0.append(False)
        except RuntimeError as e:
            cond0.append(False)
        # except:
        #     breakpoint()
    tokens_eqs = tokens_eqs[cond0]
    num_tensors = torch.cat(vals, axis=0)
    cond = (
        torch.sum(torch.count_nonzero(torch.isnan(num_tensors), dim=2), dim=1)
        < curr_p / 25
    )
    num_fil_nan = num_tensors[cond]
    tokens_eqs = tokens_eqs[cond]
    cond2 = (
        torch.sum(
            torch.count_nonzero(torch.abs(num_fil_nan) > 5e4, dim=2), dim=1
        )  # Luca comment 0n 21/01
        < curr_p / 25
    )
    num_fil_nan_big = num_fil_nan[cond2]
    tokens_eqs = tokens_eqs[cond2]
    idx = torch.argsort(num_fil_nan_big[:, 3, :]).unsqueeze(1).repeat(1, 4, 1)
    res = torch.gather(num_fil_nan_big, 2, idx)
    # res, _ = torch.sort(num_fil_nan_big)
    res = res[:, :, torch.sum(torch.count_nonzero(torch.isnan(res), dim=1), dim=0) == 0]
    res = res[
        :,
        :,
        torch.sum(torch.count_nonzero(torch.abs(res) > 5e4, dim=1), dim=0)
        == 0,  # Luca comment 0n 21/01
    ]
    return res, tokens_eqs


class DataModule(pl.LightningDataModule):
    def __init__(
        self,
        # data_train_path,
        # data_val_path,
        # data_test_path,
        cfg
    ):
        super().__init__()
        self.cfg = cfg
        # self.val_path = val_path
        # self.test_path = test_path
        #self.env_path = env_path
        # self.datamodule_params_train = cfg.datamodule_params_train
        # self.datamodule_params_val = cfg.datamodule_params_val
        # self.datamodule_params_test = cfg.datamodule_params_test
        # self.data_train_path = data_train_path
        # self.data_val_path = data_val_path 
        # self.data_test_path = data_test_path 


    def setup(self, stage=None):
        """called one ecah GPU separately - stage defines if we are at fit or test step"""
        # we set up only relevant datasets when stage is specified (automatically set by Pytorch-Lightning)
        if stage == "fit" or stage is None:
            if self.data_train_path:
                self.training_dataset = NesymresDataset(
                    self.cfg.train_path,
                    self.cfg.dataset_train,
                    mode="train"
                )
            
            if self.data_val_path:
                self.validation_dataset = NesymresDataset(
                    self.cfg.val_path,
                    self.cfg.dataset_val,
                    mode="val"
                )
            
            if self.data_test_path:
                self.test_dataset = NesymresDataset(
                    self.cfg.test_path, self.cfg.dataset_test,
                    mode="test"
                )

    def train_dataloader(self):
        """returns training dataloader"""
        trainloader = torch.utils.data.DataLoader(
            self.training_dataset,
            batch_size=self.cfg.batch_size,
            shuffle=True,
            drop_last=True,
            collate_fn=partial(custom_collate_fn,cfg= self.cfg.dataset_train),
            num_workers=self.cfg.num_of_workers,
            pin_memory=True
        )
        return trainloader

    def val_dataloader(self):
        """returns validation dataloader"""
        validloader = torch.utils.data.DataLoader(
            self.validation_dataset,
            batch_size=self.cfg.batch_size,
            shuffle=False,
            collate_fn=partial(custom_collate_fn,cfg= self.cfg.dataset_val),
            num_workers=self.cfg.num_of_workers,
            pin_memory=True
        )
        return validloader

    def test_dataloader(self):
        """returns validation dataloader"""
        testloader = torch.utils.data.DataLoader(
            self.test_dataset,
            batch_size=1,
            shuffle=False,
            collate_fn=partial(custom_collate_fn,cfg=self.cfg.dataset_test),
            num_workers=self.cfg.num_of_workers,
            pin_memory=True
        )

        return testloader
