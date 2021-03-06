import torch
import torch.nn as nn
import torch.nn.functional as F
# from .roi_layers import ROIAlign, ROIPool
from torchvision.ops.roi_align import RoIAlign as ROIAlign
from utils.util import *
from utils.bilinear import *


def conv2d(in_feat, out_feat, kernel_size=3, stride=1, pad=1, spectral_norm=True):
    conv = nn.Conv2d(in_feat, out_feat, kernel_size, stride, pad)
    if spectral_norm:
        return nn.utils.spectral_norm(conv, eps=1e-4)
    else:
        return conv


class ResnetDiscriminator128(nn.Module):
    def __init__(self, num_classes=0, input_dim=3, ch=64):
        super(ResnetDiscriminator128, self).__init__()
        self.num_classes = num_classes

        # img path
        self.block1 = OptimizedBlock(3, ch, downsample=False)
        self.block2 = ResBlock(ch, ch * 2, downsample=True)
        self.block3 = ResBlock(ch * 2, ch * 4, downsample=True)
        self.block4 = ResBlock(ch * 4, ch * 8, downsample=True)
        self.block5 = ResBlock(ch * 8, ch * 16, downsample=True)
        self.block6 = ResBlock(ch * 16, ch * 16, downsample=False)
        self.l7 = nn.utils.spectral_norm(nn.Linear(ch * 16, 1))
        self.activation = nn.ReLU()

        # obj path
        self.roi_align_s = ROIAlign((8, 8), 1.0 / 4.0, int(0))
        self.roi_align_l = ROIAlign((8, 8), 1.0 / 8.0, int(0))

        self.block_obj3 = ResBlock(ch * 2, ch * 4, downsample=False)
        self.block_obj4 = ResBlock(ch * 4, ch * 8, downsample=False)
        self.block_obj5 = ResBlock(ch * 8, ch * 16, downsample=True)
        self.l_obj = nn.utils.spectral_norm(nn.Linear(ch * 16, 1))
        self.l_y = nn.utils.spectral_norm(nn.Embedding(num_classes, ch * 16))

        # rot path
        # self.rot_embedding = torch.nn.Embedding(181, ch * 16)
        # self.r_a = nn.utils.spectral_norm(nn.Embedding(4, ch * 16))

    def forward(self, x, y=None, bbox=None):
        # print('In discriminator fwd ')
        b = bbox.size(0)
        # 128x128
        # print('block 1 start')
        # print(' x= ', x.shape)
        x = self.block1(x)
        # print('block 1 end ')
        # 64x64
        x1 = self.block2(x)
        # 32x32
        x2 = self.block3(x1)
        # 16x16
        x = self.block4(x2)
        # 8x8
        x = self.block5(x)
        # 4x4
        x = self.block6(x)
        x = self.activation(x)
        x = torch.sum(x, dim=(2, 3))
        out_im = self.l7(x)

        # obj path
        # separate path
        s_idx = (bbox[:, 3] < 64) * (bbox[:, 4] < 64)
        # PyTorch mask inversion https://github.com/OpenNMT/OpenNMT-py/issues/1524
        # bbox_l, bbox_s = bbox[1-s_idx], bbox[s_idx]
        bbox_l, bbox_s = bbox[~s_idx], bbox[s_idx]
        y_l, y_s = y[~s_idx], y[s_idx]

        obj_feat_s = self.block_obj3(x1)
        obj_feat_s = self.block_obj4(obj_feat_s)
        obj_feat_s = self.roi_align_s(obj_feat_s, bbox_s)

        obj_feat_l = self.block_obj4(x2)
        obj_feat_l = self.roi_align_l(obj_feat_l, bbox_l)

        obj_feat = torch.cat([obj_feat_l, obj_feat_s], dim=0)
        y = torch.cat([y_l, y_s], dim=0)

        obj_feat = self.block_obj5(obj_feat)
        obj_feat = self.activation(obj_feat)
        obj_feat = torch.sum(obj_feat, dim=(2, 3))  # [466, 1024]
        out_obj = self.l_obj(obj_feat)

        # why labels are fed to spectral norm?
        out_obj = out_obj + torch.sum(self.l_y(y).view(b, -1) * obj_feat.view(b, -1),
                                      dim=1, keepdim=True)
        # rot path
        # r_l, r_s = r[~s_idx], r[s_idx]
        # r = torch.cat([r_l, r_s], dim=0)  # [466, 4]
        # # r = self.rot_embedding(r)
        # # r = r.to(torch.long)
        # rot_obj = out_obj + torch.sum(self.r_a(r).view(b, -1) * obj_feat.view(b, -1),
        #                               dim=1, keepdim=True)

        return out_im, out_obj  # , rot_obj


class OptimizedBlock(nn.Module):
    def __init__(self, in_ch, out_ch, ksize=3, pad=1, downsample=False):
        super(OptimizedBlock, self).__init__()
        self.conv1 = conv2d(in_ch, out_ch, ksize, 1, pad)
        self.conv2 = conv2d(out_ch, out_ch, ksize, 1, pad)
        self.c_sc = conv2d(in_ch, out_ch, 1, 1, 0)
        self.activation = nn.ReLU()
        self.downsample = downsample

    def forward(self, in_feat):
        # print('in_feat ', in_feat.shape)
        x = in_feat
        # print('x.shape ', x.shape)
        # print('x.activation (conv1)')
        x = self.conv1(x)
        x = self.activation(x)
        # print('conv1 done')
        x = self.conv2(x)
        # print('conv 2 done')
        if self.downsample:
            x = F.avg_pool2d(x, 2)
        return x + self.shortcut(in_feat)

    def shortcut(self, x):
        if self.downsample:
            x = F.avg_pool2d(x, 2)
        return self.c_sc(x)


class ResBlock(nn.Module):
    def __init__(self, in_ch, out_ch, ksize=3, pad=1, downsample=False):
        super(ResBlock, self).__init__()
        self.conv1 = conv2d(in_ch, out_ch, ksize, 1, pad)
        self.conv2 = conv2d(out_ch, out_ch, ksize, 1, pad)
        self.activation = nn.ReLU()
        self.downsample = downsample
        self.learnable_sc = (in_ch != out_ch) or downsample
        if self.learnable_sc:
            self.c_sc = conv2d(in_ch, out_ch, 1, 1, 0)

    def residual(self, in_feat):
        x = in_feat
        x = self.conv1(self.activation(x))
        x = self.conv2(self.activation(x))
        if self.downsample:
            x = F.avg_pool2d(x, 2)
        return x

    def shortcut(self, x):
        if self.learnable_sc:
            x = self.c_sc(x)
            if self.downsample:
                x = F.avg_pool2d(x, 2)
        return x

    def forward(self, in_feat):
        return self.residual(in_feat) + self.shortcut(in_feat)


class CombineDiscriminator128(nn.Module):
    def __init__(self, num_classes=10):  # 81
        super(CombineDiscriminator128, self).__init__()
        self.obD = ResnetDiscriminator128(num_classes=num_classes, input_dim=3)

    def forward(self, images, bbox, label, mask=None, rotation=None):
        idx = torch.arange(start=0, end=images.size(0),
                           device=images.device).view(images.size(0),
                                                      1, 1).expand(-1, bbox.size(1), -1).float()

        # print(bbox.shape)
        bbox[:, :, 2] = bbox[:, :, 2] + bbox[:, :, 0]
        bbox[:, :, 3] = bbox[:, :, 3] + bbox[:, :, 1]
        bbox = bbox * images.size(2)
        # print('bbox*images', bbox.shape)  # bbox*images torch.Size([8, 4, 4])
        # print('image size (2)', images.size(2)) # image size 2: 62
        # print('idx ', idx.shape) # idx  torch.Size([8, 4, 1])
        bbox = torch.cat((idx, bbox.float()), dim=2)
        # print('bbox cat', bbox.shape) ## bbox cat torch.Size([8, 4, 5])
        bbox = bbox.view(-1, 5)
        # print('bbox reshape', bbox.shape)  # bbox reshape torch.Size([32, 5])

        label = label.view(-1)

        idx = (label != 0).nonzero().view(-1)
        # print('idx ', idx.shape) # idx  torch.Size([26])
        bbox = bbox[idx]
        # print('bbox idx', bbox[idx].shape) # bbox idx torch.Size([26, 5])
        # print('bbox ', bbox.shape) # bbox  torch.Size([26, 5])
        label = label[idx]
        # rotation = rotation.view(-1).type(torch.LongTensor).cuda()  # important: embedding error without this
        # rotation = rotation[idx]
        d_out_img, d_out_obj = self.obD(images, label, bbox)  # rm rot
        return d_out_img, d_out_obj  # , d_out_rot
