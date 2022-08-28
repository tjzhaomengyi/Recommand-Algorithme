import tensorflow as tf
from tensorflow.keras.layers import *
from tensorflow.keras.models import *
from tensorflow.keras.layers import Layer
from tensorflow.python.keras.layers import Lambda

from tensorflow.python.keras.regularizers import l2

import pandas as pd
import numpy as np
from collections import namedtuple, Counter

try:
    from tensorflow.python.ops.init_ops_v2 import Zeros, glorot_normal
except ImportError:
    from tensorflow.python.ops.init_ops import Zeros, glorot_normal_initializer as glorot_normal

SparseFeat = namedtuple('SparseFeat', ['name', 'vocabulary_size', 'embedding_dim'])
DenseFeat = namedtuple('DenseFeat', ['name', 'dimension'])
VarLenSparseFeat = namedtuple('VarLenSparseFeat', ['name', 'vocabulary_size', 'embedding_dim', 'maxlen'])


def build_input_layers(feature_columns):
    input_layer_dict = {}

    for fc in feature_columns:
        if isinstance(fc, SparseFeat):
            input_layer_dict[fc.name] = Input(shape=(1,), name=fc.name)
        elif isinstance(fc, DenseFeat):
            input_layer_dict[fc.name] = Input(shape=(fc.dimension,), name=fc.name)
        elif isinstance(fc, VarLenSparseFeat):
            input_layer_dict[fc.name] = Input(shape=(fc.maxlen,), name=fc.name)

    return input_layer_dict


# 将所有的sparse特征embedding拼接
def concat_embedding_list(feature_columns, input_layer_dict, embedding_layer_dict, flatten=False):
    embedding_list = []
    for fc in feature_columns:
        _input = input_layer_dict[fc.name]  # 获取输入层
        _embed = embedding_layer_dict[fc.name]  # B x 1 x dim  获取对应的embedding层
        embed = _embed(_input)  # B x dim  将input层输入到embedding层中

        # 是否需要flatten, 如果embedding列表最终是直接输入到Dense层中，需要进行Flatten，否则不需要
        if flatten:
            embed = Flatten()(embed)

        embedding_list.append(embed)

    return embedding_list


# 构建embedding层
def build_embedding_layers(feature_columns, input_layer_dict):
    embedding_layer_dict = {}

    for fc in feature_columns:
        if isinstance(fc, SparseFeat):
            embedding_layer_dict[fc.name] = Embedding(fc.vocabulary_size, fc.embedding_dim, name='emb_' + fc.name)
        elif isinstance(fc, VarLenSparseFeat):
            embedding_layer_dict[fc.name] = Embedding(fc.vocabulary_size + 1, fc.embedding_dim, name='emb_' + fc.name,
                                                      mask_zero=True)

    return embedding_layer_dict


def inbatch_softmax_cross_entropy_with_logits(logits, item_count, item_idx):
    print(item_count)
    print(tf.squeeze(item_idx, axis=1))
    Q = tf.gather(tf.constant(item_count / np.sum(item_count), 'float32'),
                  tf.squeeze(item_idx, axis=1))
    try:
        logQ = tf.reshape(tf.math.log(Q), (1, -1))
        logits -= logQ  # subtract_log_q
        labels = tf.linalg.diag(tf.ones_like(logits[0]))
    except AttributeError:
        logQ = tf.reshape(tf.log(Q), (1, -1))
        logits -= logQ  # subtract_log_q
        labels = tf.diag(tf.ones_like(logits[0]))

    loss = tf.nn.softmax_cross_entropy_with_logits(
        labels=labels, logits=logits)
    return loss


class NegativeSampler(
    namedtuple('NegativeSampler', ['sampler', 'num_sampled', 'item_name', 'item_count', 'distortion'])):
    """ NegativeSampler
    Args:
        sampler: sampler name,['inbatch', 'uniform', 'frequency' 'adaptive',] .
        num_sampled: negative samples number per one positive sample.
        item_name: pkey of item features .
        item_count: global frequency of item .
        distortion: skew factor of the unigram probability distribution.
    """
    __slots__ = ()

    def __new__(cls, sampler, num_sampled, item_name, item_count=None, distortion=1.0, ):
        if sampler not in ['inbatch', 'uniform', 'frequency', 'adaptive']:
            raise ValueError(' `%s` sampler is not supported ' % sampler)
        if sampler in ['inbatch', 'frequency'] and item_count is None:
            raise ValueError(' `item_count` must not be `None` when using `inbatch` or `frequency` sampler')
        return super(NegativeSampler, cls).__new__(cls, sampler, num_sampled, item_name, item_count, distortion)


class InBatchSoftmaxLayer(Layer):
    def __init__(self, sampler_config, temperature=1.0, **kwargs):
        self.sampler_config = sampler_config
        self.temperature = temperature
        self.item_count = self.sampler_config['item_count']

        super(InBatchSoftmaxLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        super(InBatchSoftmaxLayer, self).build(input_shape)

    def call(self, inputs_with_item_idx, training=None, **kwargs):
        user_vec, item_vec, item_idx = inputs_with_item_idx
        print("in batch")
        print(user_vec, item_vec, item_idx)
        if item_idx.dtype != tf.int64:
            item_idx = tf.cast(item_idx, tf.int64)
        user_vec /= self.temperature
        logits = tf.matmul(user_vec, item_vec, transpose_b=True)
        loss = inbatch_softmax_cross_entropy_with_logits(logits, self.item_count, item_idx)
        return tf.expand_dims(loss, axis=1)

    def compute_output_shape(self, input_shape):
        return (None, 1)


def l2_normalize(x, axis=-1):
    return Lambda(lambda x: tf.nn.l2_normalize(x, axis))(x)


def inner_product(x, y, temperature=1.0):
    return Lambda(lambda x: tf.reduce_sum(tf.multiply(x[0], x[1])) / temperature)([x, y])


class DNN(Layer):
    """The Multi Layer Percetron
      Input shape
        - nD tensor with shape: ``(batch_size, ..., input_dim)``. The most common situation would be a 2D input with shape ``(batch_size, input_dim)``.
      Output shape
        - nD tensor with shape: ``(batch_size, ..., hidden_size[-1])``. For instance, for a 2D input with shape ``(batch_size, input_dim)``, the output would have shape ``(batch_size, hidden_size[-1])``.
      Arguments
        - **hidden_units**:list of positive integer, the layer number and units in each layer.
        - **activation**: Activation function to use.
        - **l2_reg**: float between 0 and 1. L2 regularizer strength applied to the kernel weights matrix.
        - **dropout_rate**: float in [0,1). Fraction of the units to dropout.
        - **use_bn**: bool. Whether use BatchNormalization before activation or not.
        - **output_activation**: Activation function to use in the last layer.If ``None``,it will be same as ``activation``.
        - **seed**: A Python integer to use as random seed.
    """

    def __init__(self, hidden_units, activation='relu', l2_reg=0, dropout_rate=0, use_bn=False, output_activation=None,
                 seed=1024, **kwargs):
        self.hidden_units = hidden_units
        self.activation = activation
        self.l2_reg = l2_reg
        self.dropout_rate = dropout_rate
        self.use_bn = use_bn
        self.output_activation = output_activation
        self.seed = seed

        super(DNN, self).__init__(**kwargs)

    def build(self, input_shape):
        # if len(self.hidden_units) == 0:
        #     raise ValueError("hidden_units is empty")

        input_size = input_shape[-1]
        hidden_units = [int(input_size)] + list(self.hidden_units)
        self.kernels = [self.add_weight(name='kernel' + str(i),
                                        shape=(
                                            hidden_units[i], hidden_units[i + 1]),
                                        initializer=glorot_normal(
                                            seed=self.seed),
                                        regularizer=l2(self.l2_reg),
                                        trainable=True) for i in range(len(self.hidden_units))]
        self.bias = [self.add_weight(name='bias' + str(i),
                                     shape=(self.hidden_units[i],),
                                     initializer=Zeros(),
                                     trainable=True) for i in range(len(self.hidden_units))]
        if self.use_bn:
            self.bn_layers = [tf.keras.layers.BatchNormalization() for _ in range(len(self.hidden_units))]

        self.dropout_layers = [tf.keras.layers.Dropout(self.dropout_rate, seed=self.seed + i) for i in
                               range(len(self.hidden_units))]

        self.activation_layers = [tf.keras.layers.Activation(self.activation) for _ in range(len(self.hidden_units))]

        if self.output_activation:
            self.activation_layers[-1] = tf.keras.layers.Activation(self.output_activation)

        super(DNN, self).build(input_shape)  # Be sure to call this somewhere!

    def call(self, inputs, training=None, **kwargs):

        deep_input = inputs

        for i in range(len(self.hidden_units)):
            fc = tf.nn.bias_add(tf.tensordot(
                deep_input, self.kernels[i], axes=(-1, 0)), self.bias[i])

            if self.use_bn:
                fc = self.bn_layers[i](fc, training=training)
            try:
                fc = self.activation_layers[i](fc, training=training)
            except TypeError as e:  # TypeError: call() got an unexpected keyword argument 'training'
                print("make sure the activation function use training flag properly", e)
                fc = self.activation_layers[i](fc)

            fc = self.dropout_layers[i](fc, training=training)
            deep_input = fc

        return deep_input

    def compute_output_shape(self, input_shape):
        if len(self.hidden_units) > 0:
            shape = input_shape[:-1] + (self.hidden_units[-1],)
        else:
            shape = input_shape

        return tuple(shape)

    def get_config(self, ):
        config = {'activation': self.activation, 'hidden_units': self.hidden_units,
                  'l2_reg': self.l2_reg, 'use_bn': self.use_bn, 'dropout_rate': self.dropout_rate,
                  'output_activation': self.output_activation, 'seed': self.seed}
        base_config = super(DNN, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class EmbeddingIndex(Layer):

    def __init__(self, index, **kwargs):
        self.index = index
        super(EmbeddingIndex, self).__init__(**kwargs)

    def build(self, input_shape):
        super(EmbeddingIndex, self).build(
            input_shape)  # Be sure to call this somewhere!

    def call(self, x, **kwargs):
        return tf.constant(self.index)

    def get_config(self, ):
        config = {'index': self.index, }
        base_config = super(EmbeddingIndex, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class NoMask(Layer):
    def __init__(self, **kwargs):
        super(NoMask, self).__init__(**kwargs)

    def build(self, input_shape):
        # Be sure to call this somewhere!
        super(NoMask, self).build(input_shape)

    def call(self, x, mask=None, **kwargs):
        return x

    def compute_mask(self, inputs, mask):
        return None


class PoolingLayer(Layer):

    def __init__(self, mode='mean', supports_masking=False, **kwargs):

        if mode not in ['sum', 'mean', 'max']:
            raise ValueError("mode must be sum or mean")
        self.mode = mode
        self.eps = tf.constant(1e-8, tf.float32)
        super(PoolingLayer, self).__init__(**kwargs)

        self.supports_masking = supports_masking

    def build(self, input_shape):

        super(PoolingLayer, self).build(
            input_shape)  # Be sure to call this somewhere!

    def call(self, seq_value_len_list, mask=None, **kwargs):
        if not isinstance(seq_value_len_list, list):
            seq_value_len_list = [seq_value_len_list]
        if len(seq_value_len_list) == 1:
            return seq_value_len_list[0]
        expand_seq_value_len_list = list(map(lambda x: tf.expand_dims(x, axis=-1), seq_value_len_list))
        a = Concatenate(axis=1)(expand_seq_value_len_list)
        if self.mode == "mean":
            hist = tf.math.reduce_mean(a, axis=-1, )
        if self.mode == "sum":
            hist = tf.math.reduce_sum(a, axis=-1, )
        if self.mode == "max":
            hist = tf.math.reduce_max(a, axis=-1, )
        return hist

    def get_config(self, ):
        config = {'mode': self.mode, 'supports_masking': self.supports_masking}
        base_config = super(PoolingLayer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class SampledSoftmaxLayer(Layer):
    def __init__(self, sampler_config, temperature=1.0, **kwargs):
        self.sampler_config = sampler_config
        self.temperature = temperature
        self.sampler = self.sampler_config['sampler']
        self.item_count = self.sampler_config['item_count']

        super(SampledSoftmaxLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        self.vocabulary_size = input_shape[0][0]
        self.zero_bias = self.add_weight(shape=[self.vocabulary_size],
                                         initializer=Zeros,
                                         dtype=tf.float32,
                                         trainable=False,
                                         name="bias")
        super(SampledSoftmaxLayer, self).build(input_shape)

    def call(self, inputs_with_item_idx, training=None, **kwargs):
        item_embeddings, user_vec, item_idx = inputs_with_item_idx
        if item_idx.dtype != tf.int64:
            item_idx = tf.cast(item_idx, tf.int64)
        user_vec /= self.temperature
        if self.sampler == "inbatch":
            item_vec = tf.gather(item_embeddings, tf.squeeze(item_idx, axis=1))
            logits = tf.matmul(user_vec, item_vec, transpose_b=True)
            loss = inbatch_softmax_cross_entropy_with_logits(logits, self.item_count, item_idx)

        else:
            num_sampled = self.sampler_config['num_sampled']
            if self.sampler == "frequency":
                sampled_values = tf.nn.fixed_unigram_candidate_sampler(item_idx, 1, num_sampled, True,
                                                                       self.vocabulary_size,
                                                                       distortion=self.sampler_config['distortion'],
                                                                       unigrams=np.maximum(self.item_count, 1).tolist(),
                                                                       seed=None,
                                                                       name=None)
            elif self.sampler == "adaptive":
                sampled_values = tf.nn.learned_unigram_candidate_sampler(item_idx, 1, num_sampled, True,
                                                                         self.vocabulary_size, seed=None, name=None)
            elif self.sampler == "uniform":
                try:
                    sampled_values = tf.nn.uniform_candidate_sampler(item_idx, 1, num_sampled, True,
                                                                     self.vocabulary_size, seed=None, name=None)
                except AttributeError:
                    sampled_values = tf.random.uniform_candidate_sampler(item_idx, 1, num_sampled, True,
                                                                         self.vocabulary_size, seed=None, name=None)
            else:
                raise ValueError(' `%s` sampler is not supported ' % self.sampler)

            loss = tf.nn.sampled_softmax_loss(weights=item_embeddings,
                                              biases=self.zero_bias,
                                              labels=item_idx,
                                              inputs=user_vec,
                                              num_sampled=num_sampled,
                                              num_classes=self.vocabulary_size,
                                              sampled_values=sampled_values
                                              )
        return tf.expand_dims(loss, axis=1)

    def compute_output_shape(self, input_shape):
        return (None, 1)

    def get_config(self, ):
        config = {'sampler_config': self.sampler_config, 'temperature': self.temperature}
        base_config = super(SampledSoftmaxLayer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


def get_item_embedding(item_embedding, item_input_layer):
    return Lambda(lambda x: tf.squeeze(tf.gather(item_embedding, x), axis=1))(
        item_input_layer)


def YoutubeDNN(feature_columns, n_item,
               user_dnn_hidden_units=(64, 32),
               dnn_activation='relu', dnn_use_bn=False,
               l2_reg_dnn=0, l2_reg_embedding=1e-6, dnn_dropout=0, output_activation='linear', temperature=0.05,
               sampler_config=None, seed=1024):
    """Instantiates the YoutubeDNN Model architecture.
    :param num_sampled: int, the number of classes to randomly sample per batch.
    :param user_dnn_hidden_units: list,list of positive integer or empty list, the layer number and units in each layer of user tower
    :param dnn_activation: Activation function to use in deep net
    :param dnn_use_bn: bool. Whether use BatchNormalization before activation or not in deep net
    :param l2_reg_dnn: float. L2 regularizer strength applied to DNN
    :param l2_reg_embedding: float. L2 regularizer strength applied to embedding vector
    :param dnn_dropout: float in [0,1), the probability we will drop out a given DNN coordinate.
    :param output_activation: Activation function to use in output layer
    :param temperature: float. Scaling factor.
    :param sampler_config: negative sample config.
    :param seed: integer ,to use as random seed.
    :return: A Keras model instance.
    """

    input_layer_dict = build_input_layers(feature_columns)

    input_layers = list(input_layer_dict.values())

    # 筛选出特征中的sparse特征和dense特征，方便单独处理
    sparse_feature_columns = list(filter(lambda x: isinstance(x, SparseFeat), feature_columns))
    dense_feature_columns = list(filter(lambda x: isinstance(x, DenseFeat), feature_columns))

    # 构建embedding字典
    embedding_layer_dict = build_embedding_layers(feature_columns, input_layer_dict)

    dnn_sparse_embed_input = concat_embedding_list(sparse_feature_columns, input_layer_dict, embedding_layer_dict,
                                                   flatten=True)
    user_sparse_embedding_list = dnn_sparse_embed_input[:3]
    item_sparse_embedding_list = dnn_sparse_embed_input[3:]

    user_dnn_input = Concatenate(axis=-1)(user_sparse_embedding_list)
    user_dnn_input = tf.reshape(user_dnn_input, [-1, 3, 8])

    user_dnn_out = DNN(user_dnn_hidden_units, dnn_activation, l2_reg_dnn, dnn_dropout,
                       dnn_use_bn, output_activation=output_activation, seed=seed)(user_dnn_input)
    user_dnn_out = l2_normalize(user_dnn_out)

    user_vector_sum = Lambda(lambda x: tf.reduce_sum(x, axis=1, keepdims=False))(user_dnn_input)
    # print(user_vector_sum)
    user_vector_sum = l2_normalize(user_vector_sum)

    item_index = EmbeddingIndex(list(range(n_item)))(input_layer_dict["movie_id"])

    item_embedding_matrix = embedding_layer_dict["movie_id"]
    item_embedding_weight = NoMask()(item_embedding_matrix(item_index))

    pooling_item_embedding_weight = PoolingLayer()([item_embedding_weight])

    pooling_item_embedding_weight = l2_normalize(pooling_item_embedding_weight)
    print("pooling")
    print(pooling_item_embedding_weight)
    print("user_vector_sum")
    print(user_vector_sum)
    output = SampledSoftmaxLayer(sampler_config._asdict(), temperature)(
        [pooling_item_embedding_weight, user_vector_sum, input_layer_dict["movie_id"]])
    output = tf.sigmoid(output)
    output = tf.reshape(output, (-1, 1))
    model = Model(input_layers, output)

    model.__setattr__("user_embedding", user_dnn_out)

    # model.__setattr__("item_embedding",
    #                   get_item_embedding(pooling_item_embedding_weight, input_layer_dict["movie_id"]))

    return model


if __name__ == "__main__":
    # 读取数据

    samples_data = pd.read_csv("data/movie_sample.txt", sep="\t", header=None)
    print(samples_data.shape)
    samples_data.columns = ["user_id", "gender", "age", "hist_movie_id", "hist_len", "movie_id", "movie_type_id",
                            "label"]

    # samples_data = shuffle(samples_data)

    X = samples_data[["user_id", "gender", "age", "hist_movie_id", "hist_len", "movie_id", "movie_type_id"]]
    y = samples_data["label"]

    X_train = {"user_id": np.array(X["user_id"]), \
               "gender": np.array(X["gender"]), \
               "age": np.array(X["age"]), \
               "hist_len": np.array(X["hist_len"]), \
               "movie_id": np.array(X["movie_id"]), \
               "movie_type_id": np.array(X["movie_type_id"])}

    y_train = np.array(y)

    feature_columns = [SparseFeat('user_id', max(samples_data["user_id"]) + 1, embedding_dim=8),
                       SparseFeat('gender', max(samples_data["gender"]) + 1, embedding_dim=8),
                       SparseFeat('age', max(samples_data["age"]) + 1, embedding_dim=8),
                       SparseFeat('movie_id', max(samples_data["movie_id"]) + 1, embedding_dim=8),
                       SparseFeat('movie_type_id', max(samples_data["movie_type_id"]) + 1, embedding_dim=8),
                       DenseFeat('hist_len', 1)]

    print(X_train)
    n_users = max(samples_data["user_id"]) + 1
    n_item = max(samples_data["movie_id"]) + 1

    train_counter = Counter(X_train["movie_id"])

    item_count = [train_counter.get(i, 0) for i in range(n_item)]

    sampler_config = NegativeSampler('inbatch', num_sampled=5, item_name='movie_id', item_count=item_count)

    fm = YoutubeDNN(feature_columns, n_item, sampler_config=sampler_config)
    #
    fm.compile('adam',
               loss=tf.keras.losses.BinaryCrossentropy(),
               metrics=[tf.keras.metrics.BinaryAccuracy(),
                        tf.keras.metrics.AUC()])
    fm.fit(X_train, y_train, batch_size=64, epochs=10, validation_split=0.2, )
    #
    print(fm.summary())
