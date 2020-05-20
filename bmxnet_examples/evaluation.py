# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import division

import math
import sys
from functools import reduce
from operator import mul

import mxnet as mx
import numpy as np
import tqdm as tqdm
from mxnet import gluon

from image_classification import get_data_iters, get_model
from util.arg_parser import get_parser, set_dummy_training_args


def convert_size(size_bytes):
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])


def prepare_net(opt, net, gluon=False):
    fp_weights = 0
    binary_weights = 0

    binary_params = []
    full_precision_params = []

    all_params = net.collect_params() if gluon else net.params

    for param_name in all_params.keys():
        param = all_params[param_name]
        param_data = param.data()
        num_params = reduce(mul, param_data.shape, 1)
        if "qconv" in param_name:
            signed = param_data.det_sign()
            param.set_data(signed)
            binary_weights += num_params
            binary_params.append(param_name)
        else:
            fp_weights += num_params
            full_precision_params.append(param_name)
    bits_required = binary_weights + fp_weights * 32
    bytes_required = bits_required / 8
    if opt.verbose:
        print("full-precision weights: {}".format(fp_weights))
        print("binary weights: {} ({:.2f}% of weights are binary)".format(
            binary_weights, 100 * binary_weights / (fp_weights + binary_weights))
        )
        print("compressed model size : ~{} ({:.2f}% binary)".format(
            convert_size(bytes_required), 100 * binary_weights / bits_required)
        )
        print("binary params: {}".format(" ".join(binary_params)))
        print("full precision params: {}".format(" ".join(full_precision_params)))


if __name__ == '__main__':
    parser = get_parser(training=False)
    opt, unknown_args = parser.parse_known_args()

    set_dummy_training_args(opt)
    opt.resume = opt.params

    context = [mx.gpu(int(i)) for i in opt.gpus.split(',')] if opt.gpus.strip() else [mx.cpu()]
    ctx = context[0]

    if opt.mode == "symbolic":
        symbol_file = "{}-symbol.json".format(opt.params[:-12])
        if not os.path.isfile(symbol_file):
            print("Symbol file not found, expected at {}".format(symbol_file))
            sys.exit(1)
        net = gluon.nn.SymbolBlock.imports(symbol_file, ['data'], param_file=opt.params, ctx=ctx)
        if "binarized" not in opt.params:
            print("Warning: No 'binarized' in param name, replacing weight values with -1, +1.")
            prepare_net(opt, net)
    else:
        net, _, _ = get_model(opt, ctx)
        net.collect_params().reset_ctx(ctx)
        prepare_net(opt, net, True)

    # from PIL import ImageFont, Image, ImageDraw
    # from datasets.imagenet import MEAN_RGB, STD_RGB
    # font = ImageFont.truetype("/usr/share/fonts/truetype/hack/Hack-Regular.ttf", size=20)

    params1 = {k: v.data().asnumpy().copy() for k, v in net.collect_params().items()}

    num_correct = 0
    num_wrong = 0

    _, val_data, batch_fn = get_data_iters(opt)

    val_samples = 50000
    expected_its = val_samples // opt.batch_size
    if opt.limit_eval >= 0:
        expected_its = opt.limit_eval

    for i, batch in enumerate(tqdm.tqdm(val_data, total=expected_its)):
        if opt.limit_eval == i:
            break
        padding = 0
        if opt.dataset == "imagenet" and num_correct + num_wrong + opt.batch_size >= 50000:
            # fix validation "padding"
            padding = (num_correct + num_wrong + opt.batch_size) - 50000

        # we only use one GPU (or the CPU)
        data, label = batch_fn(batch, ctx=[ctx])
        data = data[0]
        label = label[0]

        result = net(data)
        probabilities = result.softmax().asnumpy()
        ground_truth = label.asnumpy()

        predictions = np.argmax(probabilities, axis=1)
        likeliness = np.max(probabilities, axis=1)

        if padding > 0:
            predictions = predictions[:-1 * padding]
            ground_truth = ground_truth[:-1 * padding]

        num_correct += np.sum(predictions == ground_truth)
        num_wrong += np.sum(predictions != ground_truth)

        # from datasets.imagenet_classes import CLASSES
        # for j in range(opt.batch_size):
        #     mean = mx.ndarray.array(MEAN_RGB, ctx=ctx).reshape([3, 1, 1])
        #     std = mx.ndarray.array(STD_RGB, ctx=ctx).reshape([3, 1, 1])
        #     data_unnormalized = (data[j] * std) + mean
        #     transformed = data_unnormalized.asnumpy().astype(np.uint8).transpose(1, 2, 0)
        #     canvas = Image.new("RGB", (600, 650))
        #     image = Image.fromarray(transformed, "RGB")
        #     image = image.resize((600, 600))
        #     canvas.paste(image, (0, 50))
        #     draw = ImageDraw.ImageDraw(canvas)
        #     draw.text((0, 0),  "Actual:     {}".format(CLASSES[ground_truth[j]]), font=font,
        #               fill="white")
        #     draw.text((0, 25), "Prediction: {} ({:.0f}%)".format(CLASSES[predictions[j]], likeliness[j] * 100), font=font,
        #               fill="green" if predictions[j] == ground_truth[j] else "red")
        #     canvas.save("predictions/{}.png".format(j + i * opt.batch_size))

    params2 = {k: v.data().asnumpy().copy() for k, v in net.collect_params().items()}
    for key in params1:
        np.testing.assert_almost_equal(params1[key], params2[key])

    print("Correct: {:d}, Wrong: {:d}".format(num_correct, num_wrong))
    print("Accuracy: {:.2f}%".format(100 * num_correct / (num_correct + num_wrong)))
