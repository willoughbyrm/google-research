# coding=utf-8
# Copyright 2021 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint:disable=line-too-long
r"""Converts distilled models to TFLite by iterating over experiment folders.

The aim of this file is:

1. To get TFLite models corresponding to the trained models, but only returning
the embedding (and not the target output used during training).

"""
# pylint:enable=line-too-long

import os

from absl import app
from absl import flags

import tensorflow as tf

from non_semantic_speech_benchmark.distillation import models
from non_semantic_speech_benchmark.distillation.compression_lib import compression_op as compression
from non_semantic_speech_benchmark.distillation.compression_lib import compression_wrapper

flags.DEFINE_string(
    'experiment_dir', None,
    '(CNS) Directory containing directories with parametrized names like '
    '"1-al=1.0,ap=False,bd=2048,cop=False,lr=0.0001,ms=small,qat=False,tbs=512". '
    'Note that only the mentioned hyper-params are supported right now.')
flags.DEFINE_string('output_dir', None, 'Place to write models to.')
flags.DEFINE_string('checkpoint_number', None, 'Optional checkpoint number to '
                    'use, instead of most recent.')
flags.DEFINE_boolean('quantize', False,
                     'Whether to quantize converted models if possible.')
flags.DEFINE_boolean('include_frontend', False, 'Whether to include frontend.')

FLAGS = flags.FLAGS


def get_params(experiment_dir_str):
  """Extracts hyperparams from experiment directory string.

  Args:
    experiment_dir_str: The folder-name for the set of hyperparams. Eg:
      '1-al=1.0,ap=False,bd=2048,cop=False,lr=0.0001,ms=small,qat=False,tbs=512'

  Returns:
    A dict mapping param key (str) to eval'ed value (float/eval/string).
  """
  parsed_params = {}
  start_idx = experiment_dir_str.find('-') + 1
  for kv in experiment_dir_str[start_idx:].split(','):
    key, value = kv.split('=')
    try:
      value = eval(value)  # pylint: disable=eval-used
    except:  # pylint: disable=bare-except
      pass
    parsed_params[key] = value
  return parsed_params


def get_default_compressor():
  compression_params = compression.CompressionOp.get_default_hparams().parse('')
  compressor = compression_wrapper.get_apply_compression(
      compression_params, global_step=0)
  return compressor


def get_tflite_friendly_model(checkpoint_folder_path, params,
                              checkpoint_number=None, include_frontend=False):
  """Given folder & training params, exports SavedModel without frontend."""
  compressor = None
  if params['cop']:
    compressor = get_default_compressor()
  static_model = models.get_keras_model(
      bottleneck_dimension=params['bd'],
      output_dimension=0,  # Don't include the unnecessary final layer.
      alpha=params['al'],
      mobilenet_size=params['ms'],
      frontend=include_frontend,
      avg_pool=params['ap'],
      compressor=compressor,
      quantize_aware_training=params['qat'],
      tflite=True)
  checkpoint = tf.train.Checkpoint(model=static_model)
  if checkpoint_number:
    checkpoint_to_load = os.path.join(
        checkpoint_folder_path, f'ckpt-{checkpoint_number}')
    assert tf.train.load_checkpoint(checkpoint_to_load)
  else:
    checkpoint_to_load = tf.train.latest_checkpoint(checkpoint_folder_path)
  checkpoint.restore(checkpoint_to_load).expect_partial()
  return static_model


def convert_tflite_model(model, quantize, model_path):
  """Uses TFLiteConverter to convert a Keras Model.

  Args:
    model: Keras model obtained from get_tflite_friendly_model.
    quantize: Whether to quantize TFLite model using dynamic quantization. See:
      https://www.tensorflow.org/lite/performance/post_training_quant
    model_path: Path for TFLite file.
  """
  converter = tf.lite.TFLiteConverter.from_keras_model(model)
  if quantize:
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
  tflite_buffer = converter.convert()

  with tf.io.gfile.GFile(model_path, 'wb') as f:
    f.write(tflite_buffer)


def main(_):
  tf.compat.v2.enable_v2_behavior()
  if not tf.io.gfile.exists(FLAGS.output_dir):
    tf.io.gfile.makedirs(FLAGS.output_dir)

  # Get experiment dirs names.
  # NOTE: This assumes that only folders with hyperparams in their name occur
  #       in the working dict.
  if not tf.io.gfile.exists(FLAGS.experiment_dir):
    raise ValueError(f'Experiment dir doesn\'t exist: {FLAGS.experiment_dir}')
  subdirs = tf.io.gfile.walk(FLAGS.experiment_dir)
  for subdir in subdirs:
    if subdir[0] == FLAGS.experiment_dir:
      experiment_dirs = subdir[1]
      break

  # Generate params & TFLite experiment dir names.
  experiment_dir_to_params = {}
  # Maps experiment dir name to [float model, quantized model] paths.
  experiment_dir_to_model = {}
  i = 0
  for experiment_dir in experiment_dirs:
    print('Working on hyperparams: ' + experiment_dir)
    i += 1
    params = get_params(experiment_dir)
    experiment_dir_to_params[experiment_dir] = params
    folder_path = os.path.join(FLAGS.experiment_dir, experiment_dir)

    # Export SavedModel & convert to TFLite
    # Note that we keep over-writing the SavedModel while converting experiments
    # to TFLite, since we only care about the final flatbuffer models.
    static_model = get_tflite_friendly_model(
        checkpoint_folder_path=folder_path,
        params=params,
        checkpoint_number=FLAGS.checkpoint_number,
        include_frontend=FLAGS.include_frontend)
    quantize = params['qat']
    tflite_model = os.path.join(FLAGS.output_dir, 'model_{}.tflite'.format(i))
    convert_tflite_model(
        static_model, quantize=quantize, model_path=tflite_model)
    experiment_dir_to_model[experiment_dir] = tflite_model
    if quantize:
      print('Exported INT8 TFLite model')
    else:
      print('Exported FP32 TFLite model')
  print('Total TFLite models generated: {}'.format(i))


if __name__ == '__main__':
  flags.mark_flags_as_required(['experiment_dir', 'output_dir'])
  app.run(main)
