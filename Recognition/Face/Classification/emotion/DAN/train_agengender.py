import os
import warnings
from tqdm import tqdm
import argparse

from PIL import Image
import numpy as np
import pandas as pd

import torch
import torch.nn as nn
import torch.utils.data as data
from torchvision import transforms


from sklearn.metrics import balanced_accuracy_score

from networks.dan import DAN

import random

def warn(*args, **kwargs):
    pass
warnings.warn = warn

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_path', type=str, default="./train_dataset/210602/aligned_0610_{}.txt", help='dataset path.')
    parser.add_argument('--save_base',type=str,default="./checkpoints_agengender_211108/")
    parser.add_argument('--batch_size', type=int, default=256, help='Batch size.')
    parser.add_argument('--lr', type=float, default=0.1, help='Initial learning rate for sgd.')
    parser.add_argument('--workers', default=0, type=int, help='Number of data loading workers.')
    parser.add_argument('--epochs', type=int, default=40, help='Total training epochs.')
    parser.add_argument('--num_head', type=int, default=4, help='Number of attention head.')

    return parser.parse_args()


class RafDataSet(data.Dataset):
    def __init__(self, data_path, phase, transform = None):
        self.phase = phase
        self.transform = transform
        self.data_path = data_path

        label_list = ["1_1","1_2","1_3","1_4","1_5","1_6","1_7","1_8",
                "2_1","2_2","2_3","2_4","2_5","2_6","2_7","2_8"]

        if phase=='train':
            data_path = (self.data_path).format('train')
        else:
            data_path = (self.data_path).format('val')

        lines = open(data_path,'r',encoding='utf-8').readlines()
        random.shuffle(lines)

        self.file_paths=[]
        self.label=[]
        
        for line in lines:
            line = line.strip().split(",")
            self.file_paths.append(line[0])
            self.label.append(label_list.index(line[1]))

    def __len__(self):
        return len(self.file_paths)

    def __getitem__(self, idx):
        path = self.file_paths[idx]
        image = Image.open(path).convert('RGB')
        label = self.label[idx]

        if self.transform is not None:
            image = self.transform(image)
        
        return image, label

class AffinityLoss(nn.Module):
    def __init__(self, device, num_class=8, feat_dim=512):
        super(AffinityLoss, self).__init__()
        self.num_class = num_class
        self.feat_dim = feat_dim
        self.gap = nn.AdaptiveAvgPool2d(1)
        self.device = device

        self.centers = nn.Parameter(torch.randn(self.num_class, self.feat_dim).to(device))

    def forward(self, x, labels):
        x = self.gap(x).view(x.size(0), -1)

        batch_size = x.size(0)
        distmat = torch.pow(x, 2).sum(dim=1, keepdim=True).expand(batch_size, self.num_class) + \
                  torch.pow(self.centers, 2).sum(dim=1, keepdim=True).expand(self.num_class, batch_size).t()
        distmat.addmm_(x, self.centers.t(), beta=1, alpha=-2)

        classes = torch.arange(self.num_class).long().to(self.device)
        labels = labels.unsqueeze(1).expand(batch_size, self.num_class)
        mask = labels.eq(classes.expand(batch_size, self.num_class))

        dist = distmat * mask.float()
        dist = dist / self.centers.var(dim=0).sum()

        loss = dist.clamp(min=1e-12, max=1e+12).sum() / batch_size

        return loss

class PartitionLoss(nn.Module):
    def __init__(self, ):
        super(PartitionLoss, self).__init__()
    
    def forward(self, x):
        num_head = x.size(1)

        if num_head > 1:
            var = x.var(dim=1).mean()
            loss = torch.log(1+num_head/var)
        else:
            loss = 0
            
        return loss

def run_training():
    args = parse_args()
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    if not os.path.exists(args.save_base):
        os.mkdir(args.save_base)

    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.enabled = True

    model = DAN(num_head=args.num_head,num_class=16)
    model.to(device)

    data_transforms = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomApply([
                transforms.RandomRotation(20),
                transforms.RandomCrop(224, padding=32)
            ], p=0.2),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225]),
        transforms.RandomErasing(scale=(0.02,0.25)),
        ])
    
    train_dataset = RafDataSet(args.data_path, phase = 'train', transform = data_transforms)    
    
    print('Whole train set size:', train_dataset.__len__())

    train_loader = torch.utils.data.DataLoader(train_dataset,
                                               batch_size = args.batch_size,
                                               num_workers = args.workers,
                                               shuffle = True,  
                                               pin_memory = True)

    data_transforms_val = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225])])   

    val_dataset = RafDataSet(args.data_path, phase = 'test', transform = data_transforms_val)   

    print('Validation set size:', val_dataset.__len__())
    
    val_loader = torch.utils.data.DataLoader(val_dataset,
                                               batch_size = args.batch_size,
                                               num_workers = args.workers,
                                               shuffle = False,  
                                               pin_memory = True)

    criterion_cls = torch.nn.CrossEntropyLoss()
    criterion_af = AffinityLoss(device)
    criterion_pt = PartitionLoss()

    params = list(model.parameters()) + list(criterion_af.parameters())
    optimizer = torch.optim.SGD(params,lr=args.lr, weight_decay = 1e-4, momentum=0.9)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.1)


    best_acc = 0
    for epoch in tqdm(range(1, args.epochs + 1)):
        running_loss = 0.0
        correct_sum = 0
        iter_cnt = 0
        model.train()

        for (imgs, targets) in train_loader:
            iter_cnt += 1
            optimizer.zero_grad()

            imgs = imgs.to(device)
            targets = targets.to(device)
            
            out,feat,heads = model(imgs)

            loss = criterion_cls(out,targets) + 1* criterion_af(feat,targets) + 1*criterion_pt(heads)  #89.3 89.4

            loss.backward()
            optimizer.step()
            

            print("train iter:",iter_cnt," Loss:",loss)

            running_loss += loss
            _, predicts = torch.max(out, 1)
            correct_num = torch.eq(predicts, targets).sum()
            correct_sum += correct_num

        acc = correct_sum.float() / float(train_dataset.__len__())
        running_loss = running_loss/iter_cnt
        tqdm.write('[Epoch %d] Training accuracy: %.4f. Loss: %.3f. LR %.6f' % (epoch, acc, running_loss,optimizer.param_groups[0]['lr']))
        
        with torch.no_grad():
            running_loss = 0.0
            iter_cnt = 0
            bingo_cnt = 0
            sample_cnt = 0
            baccs = []

            model.eval()
            for (imgs, targets) in val_loader:
                imgs = imgs.to(device)
                targets = targets.to(device)
                
                out,feat,heads = model(imgs)
                loss = criterion_cls(out,targets) + criterion_af(feat,targets) + criterion_pt(heads)

                running_loss += loss
                iter_cnt+=1
                _, predicts = torch.max(out, 1)
                correct_num  = torch.eq(predicts,targets)
                bingo_cnt += correct_num.sum().cpu()
                sample_cnt += out.size(0)


                print("val iter:",iter_cnt," Loss:",loss)
                
                baccs.append(balanced_accuracy_score(targets.cpu().numpy(),predicts.cpu().numpy()))
            running_loss = running_loss/iter_cnt   
            scheduler.step()

            acc = bingo_cnt.float()/float(sample_cnt)
            acc = np.around(acc.numpy(),4)
            best_acc = max(acc,best_acc)

            bacc = np.around(np.mean(baccs),4)
            tqdm.write("[Epoch %d] Validation accuracy:%.4f. bacc:%.4f. Loss:%.3f" % (epoch, acc, bacc, running_loss))
            tqdm.write("best_acc:" + str(best_acc))


            if acc == best_acc:
        
                torch.save({'iter': epoch,
                            'model_state_dict': model.state_dict(),
                             'optimizer_state_dict': optimizer.state_dict(),},
                            os.path.join(args.save_base, "rafdb_epoch"+str(epoch)+"_acc"+str(acc)+"_bacc"+str(bacc)+".pth"))
                tqdm.write('Model saved.')

        
if __name__ == "__main__":        
    run_training()