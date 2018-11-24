import torch
from torch import nn
import torch.nn.functional as F

import math

class SETLayer(nn.Module):
    r"""Impliments an evolutionary sparse layer.

    Args:
        in_features (int): size of each input sample
        out_features (int): size of each output sample
        epsilon (int): epsilon from Erdös–Rényi 
                       random graph probability formulation
                       (proportional to number of parameters in layer)
            Default: 11
        sparsity (float): Manually set sparsity of weight matrix
                          (alternate to epsilon)
            Default: None
            
        zeta (float): proportion of connections to reset on 
                      evolution step. Default setting recommended by authors.
            Default: .3

        bias: If set to False, the layer will not learn an additive bias.
            Default: ``True``

    Attributes:
        sparsity: the sparsity of the weight matrix (directly set or
            determined by the Erdös–Rényi random graph propability distribution)
        n_params: the number of parameters in the sparse weight matrix
            `(out_features x in_features x (1-sparsity))`
        weight: weight parameters (vector of shape (n_params,))
        indim: set by `in_features`
        outdim: set by `out_features`
        
        
        connections: A torch tensor of shape (n_params, 2). Indicates
        the input node and output node for each weight parameter.
        
        bias: the learnable bias of the module of shape `(out_features)`

    Methods:
        grow_connections(self, indices=None): Randomly assigns connections
        specified by indices (indexed into parameter vector). If no indices passed,
        randomly assigns all connections 
        
        zero_connections(self): Sets parameters selected by zeta criterion
        (roughly proportion zeta of smallest magnitude weights) to zero.
        
        evolve_connections(self, init=True): Evolves connections selected by
        zeta criterion by reassigning them. If init set to True, re-initializes
        parameters with same initial distribution as init. Else, sets these parameters
        to zero.
        
        other methods are "private" (TODO: rename private methods)

    Examples::

        >>> m = SETLayer(1024, 1024)
        >>> input = torch.randn(128, 1024)
        >>> output = m(input)
        >>> print(output.size())
    """

    def __init__(self, 
                 in_features, 
                 out_features,
                 epsilon=11,
                 sparsity=None,
                 zeta=.3,
                 bias=True
                ):
        super(SETLayer, self).__init__()
        self.indim = in_features
        self.outdim = out_features
        self.epsilon = epsilon
        self.zeta = zeta
        if sparsity is not None:
            self.sparsity = sparsity
        else:
            #Erdös–Rényi random graph probability
            density = (epsilon * (self.indim + self.outdim))/(self.indim * self.outdim)
            sparsity = 1 - density
            if sparsity > 1:
                sparsity = .9
            self.sparsity = sparsity
        self.n_params = int(self.indim * self.outdim * (1 - self.sparsity))
        self.marked_indices = None
        
        self.connections = None
        self.grow_connections()
        self.generate_zmap()
        
        self.weight = nn.Parameter(torch.Tensor(self.n_params))
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_features))
        else:
            self.register_parameter('bias', None)
        self.reset_parameters()
            
    def grow_connections(self, indices=None):
        """Randomly assigns connections."""
        if indices is None:
            n_connections = self.n_params
        else:
            n_connections = len(indices)
        connections = self.generate_connections(n_connections)
        if indices is None:
            self.connections = connections
        else:
            self.connections[indices] = connections
            
    def generate_connections(self, n_connections):
        """
        Generates a set of connections randomly,
        avoids existing connections.
        number of connections is equal to the number of 
        connections needed to get len(self.prms) == n_params
        """
        if self.connections is not None:
            t = self.connections
            t = set([(int(x[0]), int(x[1])) for x in t])
        else:
            t = set()
        
        #generate extras in case of duplicates
        iidx = torch.randint(self.indim, size=(int(n_connections*1.5),)).reshape(-1, 1)
        oidx = torch.randint(self.outdim, size=(int(n_connections*1.5),)).reshape(-1, 1)
        t_ = torch.cat([iidx, oidx], dim=1).int()
        t_ = set([(int(x[0]), int(x[1])) for x in t_])
        new_locs = [torch.tensor([tup[0], tup[1]]).int().reshape(1, -1) for tup in t_ if tup not in t]
        new_locs = torch.cat(new_locs, dim=0)
        
        #if not enough unique connections, try again:
        if len(new_locs) < n_connections:
            return self.generate_connections(n_connections)
        else:
            return new_locs[:n_connections]
        
    def mark_connections(self):
        """
        Finds parameters closest to zero in proportion
        self.zeta.
        Returns a list of indices marked for death.
        """
        tens = self.weight.data

        pos = (tens > 0).int()
        numpos = int(torch.sum(pos))

        neg = (tens < 0).int()
        numneg = int(torch.sum(neg))

        neg_tokill = int(numneg * self.zeta)
        neg_k = neg_tokill + numpos

        pos_tokill = int(numpos * self.zeta)
        pos_k = pos_tokill + numneg

        vals, inds = torch.topk(tens, k=neg_k, largest=True)
        kill_neg = torch.zeros_like(tens).int()
        kill_neg[inds] = 1
        to_kill_neg = neg * kill_neg

        vals, inds = torch.topk(tens, k=pos_k, largest=False)
        kill_pos = torch.zeros_like(tens).int()
        kill_pos[inds] = 1
        to_kill_pos = pos * kill_pos

        all_to_kill = to_kill_neg + to_kill_pos
        return torch.nonzero(all_to_kill).reshape(-1)
    
    def kill_parameters(self, indices):
        """Sets specified parameters to zero."""
        if len(indices.shape) > 1:
            indices = indices.reshape(-1)
        self.weight.data[indices] = 0      
        
    def zero_connections(self):
        """Sets small parameters to zero without changing connections."""
        indices = self.mark_connections()
        self.kill_parameters(indices)
        self.marked_indices = indices
        
    def evolve_connections(self, init=True):
        """
        Performs connection evolution.
        if init set to false, connections are set to zero.
        Otherwise, connections are randomly initialized
        by sampling from same init distribution as t=0
        """
        if self.marked_indices is None:
            indices = self.mark_connections()
        else:
            indices = self.marked_indices
        if init:
            stdv = math.sqrt(2/self.indim)
            new_values = torch.randn((len(indices))) * stdv
        else:
            new_values = torch.zeros_like(indices)
        self.weight.data[indices] = new_values
        self.grow_connections(indices)
        self.generate_zmap()
        
    def generate_zmap(self):
        """Generates an indexing map for forward pass"""
        z_map = []
        longest = 0
        for i in range(self.outdim):
            prms = self.connections[:, 1] == i
            indcs = torch.nonzero(prms).reshape(-1).int()
            longest = max(longest, len(indcs))
            z_map.append(indcs)

        z_inds = (torch.ones((self.outdim, longest)) * self.n_params).int()
        for i, inds in enumerate(z_map):
            z_inds[i, :len(inds)] = inds

        self.zmap = z_inds.long()

    def reset_parameters(self):
        stdv = math.sqrt(2/self.indim)
        self.weight.data = torch.randn(self.n_params) * stdv
        if self.bias is not None:
            self.bias.data = torch.randn(self.outdim) * stdv

    def forward(self, x):
        inds = self.connections[:, 0].long()
        k = x[:, inds]
        k = k * self.weight
        k = torch.cat([k, torch.zeros(k.shape[0], 1)], 1)
        zmat = k[:, self.zmap]
        z = zmat.sum(dim=2)
        return z + self.bias