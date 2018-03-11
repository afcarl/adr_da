from __future__ import print_function
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from model.build_gen import *
from datasets.dataset_read import dataset_read


# Training settings
class Solver(object):
    def __init__(self, args, batch_size=64, source='svhn',
                 target='mnist', learning_rate=0.0002, interval=100, optimizer='adam'
                 , all_use=False, num_k=4, entropy=False, checkpoint_dir=None, save_epoch=10):
        self.batch_size = batch_size
        self.source = source
        self.target = target
        self.all_use = all_use
        self.num_k = num_k
        self.entropy = entropy
        self.checkpoint_dir = checkpoint_dir
        self.save_epoch = save_epoch
        self.use_abs_diff = args.use_abs_diff
        if self.source == 'svhn':
            self.scale = True
        else:
            self.scale = False
        print('dataset loading')
        self.datasets, self.dataset_test = dataset_read(source, target, self.batch_size, scale=self.scale,
                                                        all_use=self.all_use)
        print('load finished!')
        self.G = Generator(source=source, target=target)
        self.C1 = Classifier(source=source, target=target)
        self.C2 = Classifier(source=source, target=target)
        if args.eval_only:
            self.G.torch.load(
                '%s/%s_to_%s_model_epoch%s_G.pt' % (self.checkpoint_dir, self.source, self.target, args.resume_epoch))
            self.G.torch.load(
                '%s/%s_to_%s_model_epoch%s_G.pt' % (
                    self.checkpoint_dir, self.source, self.target, self.checkpoint_dir, args.resume_epoch))
            self.G.torch.load(
                '%s/%s_to_%s_model_epoch%s_G.pt' % (self.checkpoint_dir, self.source, self.target, args.resume_epoch))

        self.G.cuda()
        self.C1.cuda()
        self.C2.cuda()
        self.interval = interval

        self.set_optimizer(which_opt=optimizer, lr=learning_rate)
        self.lr = learning_rate

    def set_optimizer(self, which_opt='momentum', lr=0.001, momentum=0.9):
        if which_opt == 'momentum':
            self.opt_g = optim.SGD(self.G.parameters(),
                                   lr=lr, weight_decay=0.0005,
                                   momentum=momentum)

            self.opt_c1 = optim.SGD(self.C1.parameters(),
                                    lr=lr, weight_decay=0.0005,
                                    momentum=momentum)
            self.opt_c2 = optim.SGD(self.C2.parameters(),
                                    lr=lr, weight_decay=0.0005,
                                    momentum=momentum)

        if which_opt == 'adam':
            self.opt_g = optim.Adam(self.G.parameters(),
                                    lr=lr, weight_decay=0.0005)

            self.opt_c1 = optim.Adam(self.C1.parameters(),
                                     lr=lr, weight_decay=0.0005)
            self.opt_c2 = optim.Adam(self.C2.parameters(),
                                     lr=lr, weight_decay=0.0005)

    def reset_grad(self):
        self.opt_g.zero_grad()
        self.opt_c1.zero_grad()
        self.opt_c2.zero_grad()

    def ent(self, output):
        return - torch.mean(output * torch.log(output + 1e-6))

    def discrepancy(self, out1, out2):
        if not self.entropy:
            out2_t = out2.clone()
            out2_t = out2_t.detach()
            out1_t = out1.clone()
            out1_t = out1_t.detach()
            if not self.use_abs_diff:
                return (F.kl_div(F.log_softmax(out1), out2_t) + F.kl_div(F.log_softmax(out2),
                                                                     out1_t)) / 2
            else:
                return torch.mean(torch.abs(out1-out2))
        else:
            return self.ent(out1)
        

    def train(self, epoch, record_file=None):
        criterion = nn.CrossEntropyLoss().cuda()
        self.G.train()
        self.C1.train()
        self.C2.train()
        torch.cuda.manual_seed(1)

        for batch_idx, data in enumerate(self.datasets):
            img_t = data['T']
            img_s = data['S']
            label_s = data['S_label']
            if img_s.size()[0] < self.batch_size or img_t.size()[0] < self.batch_size:
                break
            img_s = img_s.cuda()
            img_t = img_t.cuda()
            imgs = Variable(torch.cat((img_s, \
                                       img_t), 0))
            label_s = Variable(label_s.cuda())

            img_s = Variable(img_s)
            img_t = Variable(img_t)
            self.reset_grad()
            feat = self.G(imgs)
            output = self.C1(feat)
            output_s = output[:self.batch_size, :]
            loss_s1 = criterion(output_s, label_s)
            loss_s1.backward()
            self.opt_g.step()
            self.opt_c1.step()

            self.reset_grad()
            feat = self.G(imgs)
            output = self.C2(feat)
            output_s = output[:self.batch_size, :]
            loss_s2 = criterion(output_s, label_s)
            loss_s2.backward()
            self.opt_c2.step()
            self.reset_grad()

            feat = self.G(imgs)

            output1 = self.C1(feat)
            output1_s = output1[:self.batch_size, :]
            output1_t = output1[self.batch_size:, :]
            output1_t = F.softmax(output1_t)

            output2 = self.C1(feat)
            output2_t = output2[self.batch_size:, :]
            output2_t = F.softmax(output2_t)
            loss = criterion(output1_s, label_s)
            loss_dis = self.discrepancy(output1_t, output2_t)
            loss -= loss_dis
            loss.backward()
            self.opt_c1.step()
            self.reset_grad()

            for i in xrange(self.num_k):
                feat_t = self.G(img_t)

                output1_t = self.C1(feat_t)
                output2_t = self.C1(feat_t)
                output1_t = F.softmax(output1_t)
                output2_t = F.softmax(output2_t)
                loss_dis = self.discrepancy(output1_t, output2_t)
                G_loss = loss_dis
                G_loss.backward()
                self.opt_g.step()
                self.reset_grad()
            output = self.G(img_s)
            output1_s = self.C1(output)
            output2_s = self.C1(output)
            output1_s = F.softmax(output1_s)
            output2_s = F.softmax(output2_s)

            output = self.G(img_t)
            output1_t = self.C1(output)
            output2_t = self.C1(output)
            output1_t = F.softmax(output1_t)
            output2_t = F.softmax(output2_t)

            loss_dis = self.discrepancy(output1_t, output2_t)
            entropy = self.ent(output1_t).detach()
            loss_dis = loss_dis.detach()
            loss_dis_s = self.discrepancy(output1_s, output2_s)
            loss_dis_s = loss_dis_s.detach()

            if batch_idx > 100:
                return batch_idx

            if batch_idx % self.interval == 0:
                print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss1: {:.6f}\t Dis: {:.6f} Dis_s: {:.6f}'.format(
                    epoch, batch_idx, 100,
                    100. * batch_idx / 70000, loss.data[0], loss_dis.data[0], loss_dis_s.data[0]))
                if record_file:
                    record = open(record_file, 'a')
                    record.write('%s %s %s\n' % (loss_dis.data[0], loss_dis_s.data[0], entropy.data[0]))
                    record.close()
        return batch_idx

    def test(self, epoch, record_file=None, save_model=False):
        self.G.eval()
        self.C1.eval()
        self.C2.eval()
        test_loss = 0
        correct1 = 0
        correct2 = 0
        size = 0
        for batch_idx, data in enumerate(self.dataset_test):
            img = data['T']
            label = data['T_label']
            img, label = img.cuda(), label.cuda()
            img, label = Variable(img, volatile=True), Variable(label)
            feat = self.G(img)
            output1 = self.C1(feat)
            output2 = self.C2(feat)
            test_loss += F.nll_loss(output1, label).data[0]
            pred1 = output1.data.max(1)[1]
            pred2 = output2.data.max(1)[1]
            k = label.data.size()[0]
            correct1 += pred1.eq(label.data).cpu().sum()
            correct2 += pred2.eq(label.data).cpu().sum()
            size += k
        test_loss = test_loss / size
        print('\nTest set: Average loss: {:.4f}, Accuracy C1: {}/{} ({:.0f}%) Accuracy C2: {}/{} ({:.0f}%) \n'.format(
            test_loss, correct1, size,
            100. * correct1 / size, correct2, size, 100. * correct2 / size))
        if save_model and epoch % self.save_epoch == 0:
            torch.save(self.G,
                       '%s/%s_to_%s_model_epoch%s_G.pt' % (self.checkpoint_dir, self.source, self.target, epoch))
            torch.save(self.C1,
                       '%s/%s_to_%s_model_epoch%s_C1.pt' % (self.checkpoint_dir, self.source, self.target, epoch))
            torch.save(self.C2,
                       '%s/%s_to_%s_model_epoch%s_C2.pt' % (self.checkpoint_dir, self.source, self.target, epoch))
        if record_file:
            record = open(record_file, 'a')
            record.write('%s %s\n' % (correct1 / size, correct2 / size))
            record.close()
