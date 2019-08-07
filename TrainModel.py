# %% Setup
import time
import os
from glob import glob
from os.path import join

import GPUtil
import numpy as np
from keras.callbacks import ModelCheckpoint, ReduceLROnPlateau
from keras.optimizers import Adam
from natsort import natsorted
from sklearn.metrics import confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.utils import class_weight

from Datagen import PngClassDataGenerator, PngDataGenerator
from HelperFunctions import (RenameWeights, get_class_datagen, get_seg_datagen,
                             get_train_params, get_val_params, WaitForGPU)
from Losses import dice_coef_loss
from Models import BlockModel2D, BlockModel_Classifier, ConvertEncoderToCED

os.environ['HDF5_USE_FILE_LOCKING'] = 'false'


rng = np.random.RandomState(seed=1)

if False:
    WaitForGPU()


# ~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~~~~~ SETUP~~~~~~~
# ~~~~~~~~~~~~~~~~~~~~~~

# Setup data
pre_train_datapath = '/data/Kaggle/nih-chest-dataset/images_resampled_sorted_into_categories/Pneumothorax/'
pre_train_negative_datapath = '/data/Kaggle/nih-chest-dataset/images_resampled_sorted_into_categories/No_Finding/'

# use normalized images
# pos_img_path = '/data/Kaggle/pos-norm-png'
# pos_mask_path = '/data/Kaggle/pos-mask-png'

# use non-normalized images with large masks
pos_img_path = '/data/Kaggle/pos-filt-png'
pos_mask_path = '/data/Kaggle/pos-filt-mask-png'

pretrain_weights_filepath = 'Pretrain_class_weights.h5'
weight_filepath = 'Kaggle_Weights_{}_{{epoch:02d}}-{{val_loss:.4f}}.h5'
best_weight_filepath = 'Best_Kaggle_Weights_{}_v4.h5'

# pre-train parameters
pre_im_dims = (512, 512)
pre_n_channels = 1
pre_batch_size = 16
pre_val_split = .15
pre_epochs = 5
pre_multi_process = False
skip_pretrain = True

# train parameters
im_dims = (512, 512)
n_channels = 1
batch_size = 4
learnRate = 1e-4
val_split = .15
epochs = [5, 20]  # epochs before and after unfreezing weights
full_epochs = 50 # epochs trained on 1024 data
multi_process = False

# model parameters
filt_nums = 16
num_blocks = 4

# datagen params
pre_train_params = get_train_params(
    pre_batch_size, pre_im_dims, pre_n_channels)
pre_val_params = get_val_params(pre_batch_size, pre_im_dims, pre_n_channels)
train_params = get_train_params(batch_size, im_dims, n_channels)
val_params = get_val_params(batch_size, im_dims, n_channels)
full_train_params = get_train_params(2, (1024, 1024), 1)
full_val_params = get_val_params(2, (1024, 1024), 1)

# %% ~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~Pre-training~~~~~~
# ~~~~~~~~~~~~~~~~~~~~~~~

if not skip_pretrain:
    print('---------------------------------')
    print('---- Setting up pre-training ----')
    print('---------------------------------')

    # Get datagens for pre-training
    pre_train_gen, pre_val_gen, class_weights = get_class_datagen(
        pre_train_datapath, pre_train_negative_datapath, pre_train_params, pre_val_params, pre_val_split)

    # Create model
    pre_model = BlockModel_Classifier(input_shape=pre_im_dims+(pre_n_channels,),
                                    filt_num=filt_nums, numBlocks=num_blocks)

    # Compile model
    pre_model.compile(Adam(), loss='binary_crossentropy', metrics=['accuracy'])

    # Create callbacks
    cb_check = ModelCheckpoint(pretrain_weights_filepath, monitor='val_loss',
                            verbose=1, save_best_only=True, save_weights_only=True, mode='auto', period=1)

    print('---------------------------------')
    print('----- Starting pre-training -----')
    print('---------------------------------')

    # Train model
    pre_history = pre_model.fit_generator(generator=pre_train_gen,
                                        epochs=pre_epochs, use_multiprocessing=pre_multi_process,
                                        workers=8, verbose=1, callbacks=[cb_check],
                                        class_weight=class_weights,
                                        validation_data=pre_val_gen)

    # Load best weights
    pre_model.load_weights(pretrain_weights_filepath)

    # Calculate confusion matrix
    print('Calculating classification confusion matrix...')
    pre_val_gen.shuffle = False
    preds = pre_model.predict_generator(pre_val_gen, verbose=1)
    labels = [pre_val_gen.labels[f] for f in pre_val_gen.list_IDs]
    y_pred = np.rint(preds)
    totalNum = len(y_pred)
    y_true = np.rint(labels)[:totalNum]
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred).ravel()

    print('----------------------')
    print('Classification Results')
    print('----------------------')
    print('True positives: {}'.format(tp))
    print('True negatives: {}'.format(tn))
    print('False positives: {}'.format(fp))
    print('False negatives: {}'.format(fn))
    print('% Positive: {:.02f}'.format(100*(tp+fp)/totalNum))
    print('% Negative: {:.02f}'.format(100*(tn+fn)/totalNum))
    print('% Accuracy: {:.02f}'.format(100*(tp+tn)/totalNum))
    print('-----------------------')

else:
    # Just create model, then load weights
    pre_model = BlockModel_Classifier(input_shape=pre_im_dims+(pre_n_channels,),
                                    filt_num=filt_nums, numBlocks=num_blocks)
    # Load best weights
    pre_model.load_weights(pretrain_weights_filepath)

# %% ~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~~~ Training ~~~~~~~
# ~~~~~~~~~~~~~~~~~~~~~~~

print('Setting up 512-training')

# convert to segmentation model
model = ConvertEncoderToCED(pre_model)

# create segmentation datagens
# using positive images only
train_gen, val_gen = get_seg_datagen(
    pos_img_path, pos_mask_path, train_params, val_params, val_split)


# Create callbacks
cur_weight_path = weight_filepath.format('512train')
best_weight_path = best_weight_filepath.format('512train')
if multi_process:
    cb_check = ModelCheckpoint(cur_weight_path, monitor='val_loss',
                               verbose=1, save_best_only=True,
                               save_weights_only=True, mode='auto', period=1)
else:
    cb_check = ModelCheckpoint(best_weight_path, monitor='val_loss',
                               verbose=1, save_best_only=True,
                               save_weights_only=True, mode='auto', period=1)

cb_plateau = ReduceLROnPlateau(
    monitor='val_loss', factor=.5, patience=3, verbose=1)

# Compile model
model.compile(Adam(lr=learnRate), loss=dice_coef_loss)

print('---------------------------------')
print('----- Starting 512-training -----')
print('---------------------------------')

history = model.fit_generator(generator=train_gen,
                              epochs=epochs[0], use_multiprocessing=multi_process,
                              workers=8, verbose=1, callbacks=[cb_plateau],
                              validation_data=val_gen)

# make all layers trainable again
for layer in model.layers:
    layer.trainable = True

# Compile model
model.compile(Adam(lr=learnRate), loss=dice_coef_loss)

print('----------------------------------')
print('--Training with unfrozen weights--')
print('----------------------------------')

history2 = model.fit_generator(generator=train_gen,
                               epochs=epochs[1], use_multiprocessing=multi_process,
                               workers=8, verbose=1, callbacks=[cb_check, cb_plateau],
                               validation_data=val_gen)
if multi_process:
    # rename best weights
    RenameWeights(best_weight_path)

# %% ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~~~ Full Size Training ~~~~~~~
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


print('Setting up 1024 training')

# make full-size model
full_model = BlockModel2D((1024, 1024, n_channels), filt_num=16, numBlocks=4)
full_model.load_weights(best_weight_path)

# Compile model
full_model.compile(Adam(lr=learnRate), loss=dice_coef_loss)

# Set weight paths
cur_weight_path = weight_filepath.format('1024train')
best_weight_path = best_weight_filepath.format('1024train')
# Create callbacks
if multi_process:
    cb_check = ModelCheckpoint(cur_weight_path, monitor='val_loss',
                               verbose=1, save_best_only=True,
                               save_weights_only=True, mode='auto', period=1)
else:
    cb_check = ModelCheckpoint(best_weight_path, monitor='val_loss',
                               verbose=1, save_best_only=True,
                               save_weights_only=True, mode='auto', period=1)
cb_plateau = ReduceLROnPlateau(
    monitor='val_loss', factor=.5, patience=3, verbose=1)

# Setup full size datagens
train_gen, val_gen = get_seg_datagen(
    pos_img_path, pos_mask_path, full_train_params, full_val_params, val_split)

print('---------------------------------')
print('---- Starting 1024-training -----')
print('---------------------------------')

# train full size model
history_full = full_model.fit_generator(generator=train_gen,
                                        epochs=full_epochs, use_multiprocessing=multi_process,
                                        workers=8, verbose=1, callbacks=[cb_check, cb_plateau],
                                        validation_data=val_gen)


# Rename best weights
if multi_process:
    RenameWeights(best_weight_path)
    time.sleep(3)

# %% make some demo images

full_model.load_weights(best_weight_path)
from VisTools import DisplayDifferenceMask
import numpy as np

for rep in range(2):
    testX, testY = val_gen.__getitem__(rep)
    preds = full_model.predict_on_batch(testX)

    for im, mask, pred in zip(testX, testY, preds):
        DisplayDifferenceMask(im[..., 0], mask[..., 0], pred[..., 0])


#%%
