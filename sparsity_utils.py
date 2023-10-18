import torch
import torch.nn as nn

import os
import csv
import copy

from utils import *
import torch.nn.functional as F

import numpy as np
import math

def export_sparsity_data(model_name, model, output_dir):
    file_path = os.path.join(output_dir, "{}_sparsity_log.csv".format(model_name))

    bFirst = True
    for name, module in model.named_modules():
        if isinstance(module, VanillaConvolutionWrapper):
            if bFirst:
                bFirst = False
                with open(file_path, mode='w') as f:
                    csv_writer = csv.writer(f)
                    csv_header = ["Layer Name", "Layer Type"]
                    csv_header += ["KERNEL*KERNEL", "Avg Zeros", "Avg Sparsity", "Avg All-zero Window Sparsity"]

                    csv_writer.writerow(csv_header)

            with open(file_path, mode='a') as f:
                csv_writer = csv.writer(f)
                new_row = [name, type(module)]
                new_row += [module.kk, module.statistics.mean.mean().item()]
                new_row += [module.statistics.mean.mean().item()/module.kk]
                new_row += [module.statistics.histograms.sum(axis = 0)[-1]/module.statistics.histograms.sum()]
                csv_writer.writerow(new_row)

            np.save(os.path.join(output_dir,"{}_{}_mean.npy".format(model_name, name)), module.statistics.mean.cpu().numpy())
            np.save(os.path.join(output_dir,"{}_{}_var.npy".format(model_name, name)), module.statistics.var.cpu().numpy())
            np.save(os.path.join(output_dir,"{}_{}_correlation.npy".format(model_name, name)), module.statistics.cor.cpu().numpy())
            np.save(os.path.join(output_dir,"{}_{}_histograms.npy".format(model_name, name)), module.statistics.histograms.cpu().numpy())
            
            if module.ma_statistics is not None:
                np.save(os.path.join(output_dir,"{}_{}_ma_mean.npy".format(model_name, name)), module.ma_statistics.mean.cpu().numpy())
                np.save(os.path.join(output_dir,"{}_{}_ma_var.npy".format(model_name, name)), module.ma_statistics.var.cpu().numpy())
                np.save(os.path.join(output_dir,"{}_{}_ma_correaltion.npy".format(model_name, name)), module.ma_statistics.cor.cpu().numpy())

class StreamDataAnalyser():
    def __init__(self, stream_num):
        self.count = 0
        self.stream_num = stream_num
        self.mean = torch.zeros(stream_num)
        self.var  = torch.zeros(stream_num)
        self.cov  = torch.zeros(stream_num, stream_num)
        self.cor  = torch.zeros(stream_num, stream_num)

        if torch.cuda.is_available():
            self.mean = self.mean.cuda()
            self.var  = self.var.cuda()
            self.cov  = self.cov.cuda()
            self.cor  = self.cor.cuda()

    # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance
    def update(self, newValues):
        self.var = self.var * self.count
        self.cov = self.cov * (self.count - 1)

        # self.sparsity = np.vstack((self.sparsity, newValues.clone().cpu().numpy()))

        assert newValues.size()[1] == self.stream_num
        self.count += newValues.size()[0]

        # newvalues - oldMean
        delta = torch.subtract(newValues, self.mean.expand_as(newValues))
        self.mean += torch.sum(delta / self.count, dim=0)
        # newvalues - newMeant
        delta2 = torch.subtract(newValues, self.mean.expand_as(newValues))

        self.var += torch.sum(delta * delta2, dim=0)
        self.cov += torch.matmul(delta.T, delta2)

        self.var = self.var / self.count # note torch,var uses N-1 by default
        assert self.count > 1
        self.cov = self.cov / (self.count - 1)
        self.cor = self.cov / torch.sqrt(torch.matmul(self.var.unsqueeze(1), self.var.unsqueeze(0))) * (self.count-1) / self.count

def total_network_sparsity(model):
    ops = []
    sparsity = []
    for name, module in model.named_modules():
        if isinstance(module, VanillaConvolutionWrapper):
            sparsity.append(module.statistics.mean.mean().item()/module.kk)
            ops.append(module.ops)

    ops = np.array(ops)
    sparsity = np.array(sparsity)
    ops = ops/ops.sum()
    return (sparsity * ops).sum()

def moving_average(a, n):
    ret = torch.cumsum(a, dim=0)
    ret[n:] = ret[n:] - ret[:-n]
    return ret[n - 1:] / n

class VanillaConvolutionWrapper(nn.Module):
    def __init__(self, conv_module):
        super(VanillaConvolutionWrapper, self).__init__()

        self.conv_module = conv_module
        self.run_reference = False
        self.kk = np.prod(self.conv_module.kernel_size)

    def forward(self, x):
        # compared with MASE implementation
        # differences are: 1) torch.nn.Unfold 2) random sample patches

        #Write data to a file
        # with open(f"input.dat", 'w') as f:
        #     f.write("\n".join([ str(i) for i in x.clone().cpu().numpy().reshape(-1).tolist() ]))

        # https://discuss.pytorch.org/t/make-custom-conv2d-layer-efficient-wrt-speed-and-memory/70175
        assert self.conv_module.padding_mode == 'zeros'
        #Zero-pad x
        x_padded = F.pad(input=x, pad=self.conv_module._reversed_padding_repeated_twice, mode='constant', value=0)

        dh, dw = self.conv_module.stride

        #Number of filter, number of channels, kernel height, kernel width
        out_channels, in_channels, kh, kw = self.conv_module.weight.shape


        groups = self.conv_module.groups
        in_channels *= groups
        batch_size = x.shape[0]

        patches = x_padded.unfold(2, kh, dh).unfold(3, kw, dw)
        h_windows = patches.shape[2]
        w_windows = patches.shape[3]
        patches = patches.expand(out_channels//groups, *patches.shape) # dims = (out_channels//groups, batch_size, in_channels, h_windows, w_windows, kh, kw)
        patches = patches.permute(1, 3, 4, 0, 2, 5, 6) # dims = ( batch_size, h_windows, w_windows, out_channels//groups, in_channels, kh, kw)
        self.ops = h_windows * w_windows * out_channels * in_channels * kh * kw

        if not hasattr(self.statistics, "histograms"):
            # NOTE: Toggle the commenting for the following 2 lines for per window
            # self.statistics.histograms = torch.zeros(in_channels//groups, h_windows, w_windows, self.kk + 1)
            self.statistics.histograms = torch.zeros(in_channels//groups, self.kk + 1)
            if torch.cuda.is_available():
                self.statistics.histograms = self.statistics.histograms.cuda()

        y = torch.zeros((batch_size, h_windows, w_windows, out_channels))
        if torch.cuda.is_available():
            y = y.cuda()

        # roll the loop to reduce memory
        self.roll_factor = 7
        if h_windows % self.roll_factor != 0:
            self.roll_factor = get_factors(h_windows)[1]
        if w_windows % self.roll_factor != 0:
            self.roll_factor = 1

        for hi, wi in np.ndindex(self.roll_factor, self.roll_factor):
            hstart = hi * (h_windows // self.roll_factor)
            hend   = (hi+1) * (h_windows // self.roll_factor)
            wstart = wi * (w_windows // self.roll_factor)
            wend   = (wi+1) * (w_windows // self.roll_factor)

            patch = patches[:,hstart:hend,wstart:wend].reshape((batch_size, h_windows//self.roll_factor, w_windows//self.roll_factor, out_channels//groups, groups, in_channels//groups, kh, kw))
            patch = patch.permute(0, 1, 2, 4, 3, 5, 6, 7) #(batch_size, h_windows//self.roll_factor, w_windows//self.roll_factor, groups, out_channels//groups, in_channels//groups, kh, kw)
            weight = self.conv_module.weight.reshape((groups, out_channels//groups, in_channels//groups, kh, kw))
            patch = patch * weight

            #----------------Zero Histogram Calculation and Update-----------------------
            #Patches Dims: (batch_size, h_windows//self.roll_factor, w_windows//self.roll_factor, groups, out_channels//groups, in_channels//groups, kh, kw)
            #Histograms Dims: (in_channels, h_windows, w_windows, self.kk)
            tmp = patch.reshape((*patch.shape[:-2], self.kk))

            num_of_zeros = self.kk - torch.count_nonzero(tmp, dim = -1) # (batch_size, h_windows//self.roll_factor, w_windows//self.roll_factor, groups, out_channels//groups, in_channels//groups)


            zeros_hists = F.one_hot(num_of_zeros, num_classes = self.kk + 1) # (batch_size, h_windows//self.roll_factor, w_windows//self.roll_factor, groups, out_channels//groups, in_channels//groups, bins)

            #All out_channels have the input feature map and therefore same sparsity, can squeeze those dimensions
            zeros_hists = zeros_hists[:, :, :, :, 0].squeeze(4) # (batch_size, h_windows//self.roll_factor, w_windows//self.roll_factor, groups, in_channels//groups, bins)

            #NOTE: Toggle the commenting for the following 5 lines for per window
            zeros_hists = zeros_hists.reshape(batch_size, h_windows//self.roll_factor, w_windows//self.roll_factor, in_channels, self.kk + 1)
            zeros_hists = zeros_hists.sum(dim = (0, 1, 2)) # (in_channels, bins)
            self.statistics.histograms += zeros_hists
            # zeros_hists = zeros_hists.sum(dim = 0) # (h_windows//self.roll_factor, w_windows//self.roll_factor, in_channels//groups, bins)
            # zeros_hists = zeros_hists.permute(2, 0, 1, 3) # (in_channels//groups, h_windows//self.roll_factor, w_windows//self.roll_factor, bins)
            # self.statistics.histograms[:,hstart:hend,wstart:wend, :] += zeros_hists

            #------------------------Average sparsity calculate and update----------------------------------
            tmp = patch.reshape((-1, self.kk))
            num_of_zeros = self.kk - torch.count_nonzero(tmp, dim=1)
            num_of_zeros = num_of_zeros.reshape((-1, self.conv_module.in_channels))
            self.statistics.update(num_of_zeros)

            #-----------------------MA Statistics-----------------------
            if self.ma_statistics is not None:
                if self.ma_data_buffer is None:
                    self.ma_data_buffer = num_of_zeros
                else:
                    self.ma_data_buffer = torch.concat((self.ma_data_buffer, num_of_zeros), dim=0)
                if self.ma_data_buffer.size()[0] > self.ma_window_size:
                    new_ma = moving_average(self.ma_data_buffer, self.ma_window_size)
                    self.ma_statistics.update(new_ma)
                    if self.ma_window_size == 1:
                        self.ma_data_buffer = None
                    else:
                        self.ma_data_buffer = self.ma_data_buffer[-(self.ma_window_size-1):]

            # patch = patch.sum(-1).sum(-1).sum(-1)
            # patch = patch.reshape(batch_size, h_windows//self.roll_factor, w_windows//self.roll_factor, out_channels)
            # y[:,hstart:hend,wstart:wend] = patch

        return self.conv_module(x)

def replace_with_vanilla_convolution(model, window_size=None):
    replace_dict = {}
    conv_layer_index = 0
    for name, module in model.named_modules():
        if isinstance(module, nn.Conv2d):#or isinstance(module, nn.Linear):
            new_module = VanillaConvolutionWrapper(copy.deepcopy(module))
            new_module.statistics = StreamDataAnalyser(module.in_channels)
            new_module.ma_window_size = window_size
            new_module.ma_data_buffer = None
            if window_size is None:
                new_module.ma_statistics = None
            else:
                new_module.ma_statistics = StreamDataAnalyser(module.in_channels)
            replace_dict[module] = new_module
            conv_layer_index += 1

    replace_modules(model, replace_dict)