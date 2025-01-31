import os
import numpy as np

import tensorrt as trt

import pycuda.driver as cuda
import pycuda.autoinit

from ...data.image import read_torchImage

import torch
import torch.nn as nn
import cv2
import re


class HostDeviceMem(object):
    def __init__(self, host_mem, device_mem):
        self.host = host_mem
        self.device = device_mem

    def __str__(self):
        return "Host:\n" + str(self.host) + "\nDevice:\n" + str(self.device)

    def __repr__(self):
        return self.__str__()

class TrtModel:
    
    def __init__(self,engine_path,max_batch_size=1,dtype=np.float32,**kwargs):
        
        self.engine_path = engine_path
        self.dtype = dtype
        self.logger = trt.Logger(trt.Logger.WARNING)
        self.runtime = trt.Runtime(self.logger)
        self.engine = self.load_engine(self.runtime, self.engine_path)
        self.max_batch_size = max_batch_size
        self.output_sort = kwargs.get("output_sort",False)

        self.inputs, self.outputs, self.bindings, self.stream = self.allocate_buffers()
        self.context = self.engine.create_execution_context()
        self.outs_len = len(self.outputs)
        self.not_norm = kwargs.get("not_norm",False)
        self.torch_image = kwargs.get("torch_image",False)
        self.device = cuda.Device(0)
        #self.ctx = self.device.make_context()

        
        
    @staticmethod
    def load_engine(trt_runtime, engine_path):
        trt.init_libnvinfer_plugins(None, "")    
        with open(engine_path, 'rb') as f:
            engine_data = f.read()
        engine = trt_runtime.deserialize_cuda_engine(engine_data)
        return engine
    
    def allocate_buffers(self):
        
        inputs = []
        outputs = []
        bindings = []
        stream = cuda.Stream()

        if self.output_sort:
            engine_list = [bind for bind in self.engine]
            engine_list = sorted(engine_list)
        else:
            engine_list = self.engine

        for binding in engine_list:
            size = trt.volume(self.engine.get_binding_shape(binding)) * self.max_batch_size
            #host_mem = cuda.pagelocked_empty(size, self.dtype)
            host_mem = cuda.pagelocked_empty(size, trt.nptype(self.engine.get_binding_dtype(binding)))
            device_mem = cuda.mem_alloc(host_mem.nbytes)
            
            bindings.append(int(device_mem))

            if self.engine.binding_is_input(binding):
                inputs.append(HostDeviceMem(host_mem, device_mem))
            else:
                outputs.append(HostDeviceMem(host_mem, device_mem))
        
        return inputs, outputs, bindings, stream
       
            
    def __call__(self,x:np.ndarray,**kwargs):
        
        not_norm = self.not_norm
        if self.torch_image:
            x = x.numpy()
        else:
            x = read_torchImage(x,not_norm=not_norm).numpy()

        '''
        if type(x)==torch.Tensor:
            x = x.numpy()
         '''
        x = x.astype(self.dtype)

        np.copyto(self.inputs[0].host,x.ravel())
        
        #self.ctx.push()

        for inp in self.inputs:
            cuda.memcpy_htod_async(inp.device, inp.host, self.stream)
        
        self.context.execute_async(batch_size=kwargs.get("batch_size",1), bindings=self.bindings, stream_handle=self.stream.handle)
        for out in self.outputs:
            cuda.memcpy_dtoh_async(out.host, out.device, self.stream) 

        
        self.stream.synchronize()
        #self.ctx.pop()
        
        return [out.host.reshape(kwargs.get("batch_size",1),-1) for out in self.outputs]



