import gizeh
import moviepy.editor as mpy
import cv2
import os
import numpy as np

base = '/home/damien/Images/resl_8/'
counter= 0
images = np.array(os.listdir(base))
images = images[np.array([int(x[:4])>4379 for x in images])]


images = images[np.argsort([int(x[:4]) for x in images])]

def make_frame(t):
    global counter, images
    im = cv2.imread(base+images[counter])
    counter += 4
    return im

clip = mpy.VideoClip(make_frame, duration=int(len(images)/100.)) # 2 seconds
clip.write_gif("canard.gif",fps=25)
