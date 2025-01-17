import functools
import typing
from typing import Sequence, Dict

import fjformer.attention
import flax.core
from flax.struct import dataclass
from jax import numpy as jnp, Array, lax
from jax.experimental.shard_map import shard_map
from jax.sharding import PartitionSpec as PS
import jax
from flax import linen as nn
from flax.traverse_util import unflatten_dict, flatten_dict
from flax.core import freeze, unfreeze, FrozenDict
from typing import Union, Optional, Tuple
from transformers import FlaxPreTrainedModel
from flax.linen import partitioning as nn_partitioning, dot_product_attention_weights

from ..flax_modelling_utils import (
    ACT2FN,
    with_sharding_constraint,
    get_gradient_checkpoint_policy,
    repeat_kv_bnsh,
    apply_rotary_pos_emb,
    precompute_freq_cis,
    JaxBaseClassModel,
    smart_flash_attention,
    get_dot_general_by_bits
)
import chex


class MixtralConfig(JaxBaseClassModel):
    model_type = "mixtral"

    def __init__(
            self,
            vocab_size=32000,
            hidden_size=4096,
            intermediate_size=14336,
            num_hidden_layers=32,
            num_attention_heads=32,
            num_key_value_heads=8,
            hidden_act="silu",
            max_position_embeddings=4096 * 32,
            initializer_range=0.02,
            rms_norm_eps=1e-5,
            use_cache=True,
            pad_token_id=None,
            bos_token_id=1,
            eos_token_id=2,
            tie_word_embeddings=False,
            rope_theta=1e6,
            sliding_window=4096,
            attention_dropout=0.0,
            num_experts_per_tok=2,
            num_local_experts=8,
            output_router_logits=False,
            router_aux_loss_coef=0.001,
            gradient_checkpointing: str = 'nothing_saveable',
            use_pjit_attention_force: bool = False,
            use_flash_attention: bool = False,
            use_sacn_mlp: bool = False,
            flash_attn_query_chunk_size: int = 1024,
            flash_attn_key_chunk_size: int = 1024,
            scan_mlp_chunk_size: int = 1024,
            number_rep_kv: int = 1,
            attn_pdrop: float = 0.0,
            c_max_position_embeddings: int = 4096,
            freq_max_position_embeddings: int = 4096,
            bits: Optional[int] = None,
            **kwargs,
    ):
        """
        The __init__ function is called when the class is instantiated.
        It allows the class to initialize the attributes of a class.
        The self parameter is a reference to the current instance of the class, and is used to access variables that belong to the class.

        :param self: Represent the instance of the class
        :param vocab_size: Define the size of the vocabulary
        :param hidden_size: Determine the size of the embedding layers
        :param intermediate_size: Define the size of the intermediate layer in each transformer block
        :param num_hidden_layers: Determine the number of layers in the encoder and decoder
        :param num_attention_heads: Determine the number of attention heads in each layer
        :param num_key_value_heads: Specify the number of heads for key and value
        :param hidden_act: Specify the activation function used in the hidden layers
        :param max_position_embeddings: Set the maximum length of the sequence
        :param initializer_range: Initialize the weights of the model
        :param rms_norm_eps: Avoid division by zero in the rms normalization
        :param use_cache: Determine whether to use the cache in the decoder
        :param pad_token_id: Specify the token id of the padding token
        :param bos_token_id: Specify the beginning of sentence token id
        :param eos_token_id: Specify the end of sentence token
        :param tie_word_embeddings: Tie the word embeddings and the output layer
        :param rope_theta: Control the number of tokens in a rope
        :param sliding_window: Control the number of tokens that are processed in parallel
        :param gradient_checkpointing: str: Specify whether to use gradient checkpointing
        :param use_pjit_attention_force: bool: Force the use of pjit attention
        :param use_flash_attention: bool: Enable the flash attention mechanism
        :param use_sacn_mlp: bool: Determine whether or not to use the scan_mlp function
        :param flash_attn_query_chunk_size: int: Determine the number of rows in each chunk
        :param flash_attn_key_chunk_size: int: Control the size of chunks that are used for the key matrix in flash attention
        :param scan_mlp_chunk_size: int: Specify the chunk size of the scan mlp
        :param number_rep_kv: int: Specify the number of times to repeat the key and value vectors
        :param attn_pdrop: float: Set the dropout rate for the attention layer
        :param c_max_position_embeddings: int: Set the maximum number of tokens in a sequence
        :param freq_max_position_embeddings: int: Set the maximum number of frequency bins that can be used in the model
        :param bits: Optional[int]: Specify the number of bits used for quantization
        :param axis_dims: Sequence[int]: Specify the dimension of each axis
        :param axis_names: Sequence[str]: Specify the names of each axis in the tensor
        :param &quot;mp&quot;): Define the maximum position embeddings
        :param **kwargs: Pass a variable number of keyword arguments to a function
        :param : Define the number of layers in the model
        :return: An instance of the class

        """
        self.vocab_size = vocab_size
        self.max_position_embeddings = max_position_embeddings
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.sliding_window = sliding_window
        self.bits = bits
        self.attention_dropout = attention_dropout
        self.num_local_experts = num_local_experts
        self.num_experts_per_tok = num_experts_per_tok
        self.output_router_logits = output_router_logits
        self.router_aux_loss_coef = router_aux_loss_coef
        # for backward compatibility
        if num_key_value_heads is None:
            num_key_value_heads = num_attention_heads

        self.num_key_value_heads = num_key_value_heads
        self.hidden_act = hidden_act
        self.initializer_range = initializer_range
        self.rms_norm_eps = rms_norm_eps
        self.use_cache = use_cache
        self.rope_theta = rope_theta
        self.use_flash_attention = use_flash_attention
        self.number_rep_kv = number_rep_kv
        self.gradient_checkpointing = gradient_checkpointing
        self.use_pjit_attention_force = use_pjit_attention_force
        self.use_sacn_mlp = use_sacn_mlp
        self.flash_attn_query_chunk_size = flash_attn_query_chunk_size
        self.flash_attn_key_chunk_size = flash_attn_key_chunk_size
        self.scan_mlp_chunk_size = scan_mlp_chunk_size
        self.attn_pdrop = attn_pdrop
        self.c_max_position_embeddings = c_max_position_embeddings
        self.freq_max_position_embeddings = freq_max_position_embeddings

        super().__init__(
            pad_token_id=pad_token_id,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            tie_word_embeddings=tie_word_embeddings,
            **kwargs,
        )

    @staticmethod
    def get_partition_rules(fully_fsdp: bool = True):
        """
        The get_partition_rules function is used to define the partitioning scheme for a model.
        It returns a list of tuples, where each tuple contains two elements:
          1) A regex string that matches the name of one or more parameters in the model.
          2) A PartitionScheme object that defines how those parameters should be partitioned.

        :param fully_fsdp: bool: Determine whether to use the fully_fsdp partitioning scheme or not
        :return: A list of tuples

        """
        return (

            ("model/embed_tokens/embedding", PS("sp", "fsdp")),

            ("self_attn/(q_proj|k_proj|v_proj)/kernel", PS("fsdp", "sp")),
            ("self_attn/o_proj/kernel", PS("sp", "fsdp")),

            ("mlp/w1/kernel", PS(("fsdp", "sp"))),
            ("mlp/w2/kernel", PS(("fsdp", "sp"))),
            ("mlp/w3/kernel", PS(("fsdp", "sp"))),

            ("input_layernorm/kernel", PS(None)),
            ("post_attention_layernorm/kernel", PS(None)),

            ("model/norm/kernel", PS(None)),
            ("lm_head/kernel", PS("fsdp", "sp")),
            ('.*', PS(None)),
        ) if not fully_fsdp else (
            ("model/embed_tokens/embedding", PS(("fsdp", "sp"))),

            ("self_attn/(q_proj|k_proj|v_proj)/kernel", PS(("fsdp", "sp"))),
            ("self_attn/o_proj/kernel", PS(("fsdp", "sp"))),

            ("mlp/w1/kernel", PS(("fsdp", "sp"))),
            ("mlp/w2/kernel", PS(("fsdp", "sp"))),
            ("mlp/w3/kernel", PS(("fsdp", "sp"))),

            ("input_layernorm/kernel", PS(None)),
            ("post_attention_layernorm/kernel", PS(None)),

            ("model/norm/kernel", PS(None)),
            ("lm_head/kernel", PS(("fsdp", "sp"))),
            ('.*', PS(("fsdp", "sp"))),
        )

    def add_jax_args(self,
                     gradient_checkpointing: str = 'nothing_saveable',
                     use_pjit_attention_force: bool = False,
                     use_flash_attention: bool = False,
                     use_sacn_mlp: bool = False,
                     flash_attn_query_chunk_size: int = 1024,
                     flash_attn_key_chunk_size: int = 1024,
                     scan_mlp_chunk_size: int = 1024,
                     number_rep_kv: int = 1,
                     attn_pdrop: float = 0.0,
                     c_max_position_embeddings: int = 4096,
                     freq_max_position_embeddings: int = None,
                     bits: Optional[int] = None,
                     **kwargs,
                     ):
        """
        The add_jax_args function adds the following arguments to the model:

        :param self: Bind the attributes and methods of a class to an instance of that class
        :param gradient_checkpointing: str: Determine whether to use gradient checkpointing
        :param use_pjit_attention_force: bool: Determine whether to use the pjit_attention_force function
        :param use_flash_attention: bool: Determine if the flash attention module is used or not
        :param use_sacn_mlp: bool: Determine whether to use the scan_mlp function or not
        :param flash_attn_query_chunk_size: int: Specify the number of tokens that will be processed at a time
        :param flash_attn_key_chunk_size: int: Chunk the keys for flash attention
        :param scan_mlp_chunk_size: int: Chunk the input to the mlp
        :param number_rep_kv: int: Control the number of times that the key and value vectors are repeated
        :param attn_pdrop: float: Set the dropout rate for the attention layer
        :param c_max_position_embeddings: int: Set the maximum number of positional embeddings for the causal axis
        :param freq_max_position_embeddings: int: Set the maximum length of the frequency axis
        :param bits: Optional[int]: Specify the number of bits to use for quantization
        :return: A tuple of the following:

        """
        self.use_flash_attention = use_flash_attention
        self.number_rep_kv = number_rep_kv
        self.gradient_checkpointing = gradient_checkpointing
        self.use_pjit_attention_force = use_pjit_attention_force
        self.use_sacn_mlp = use_sacn_mlp
        self.flash_attn_query_chunk_size = flash_attn_query_chunk_size
        self.flash_attn_key_chunk_size = flash_attn_key_chunk_size
        self.scan_mlp_chunk_size = scan_mlp_chunk_size
        self.attn_pdrop = attn_pdrop
        self.c_max_position_embeddings = c_max_position_embeddings
        self.freq_max_position_embeddings = freq_max_position_embeddings
        self.bits = bits

    @staticmethod
    def get_weight_decay_exclusions():
        return tuple()

    @staticmethod
    def rng_keys():
        return 'params', 'dropout', 'fcm'


re_mat = nn_partitioning.remat


@dataclass
class MoeModelOutput:
    last_hidden_state: chex.Array = None
    hidden_states: Optional[Tuple[chex.Array]] = None
    attentions: Optional[Tuple[chex.Array]] = None
    router_logits: Optional[Tuple[chex.Array]] = None


@dataclass
class MoeCausalLMOutput:
    aux_loss: Optional[chex.Array] = None
    logits: chex.Array = None
    hidden_states: Optional[Tuple[chex.Array]] = None
    attentions: Optional[Tuple[chex.Array]] = None
    router_logits: Optional[Tuple[chex.Array]] = None


def jax_load_balancing_loss_func(gate_logits: chex.Array, num_experts: chex.Array = None, top_k: int = 2) -> float:
    if gate_logits is None:
        return 0
    if isinstance(gate_logits, tuple):
        gate_logits = jnp.concatenate([gate for gate in gate_logits], axis=0)
    routing_weights, selected_experts = jax.lax.top_k(gate_logits, top_k)
    routing_weights = jax.nn.softmax(routing_weights, axis=-1)
    if selected_experts.dtype != jnp.int64:
        selected_experts = selected_experts.astype(jnp.int64)
    if len(selected_experts.shape) == 2:
        selected_experts = selected_experts[:, :, jnp.newaxis]
    expert_mask = jnp.max(jax.nn.one_hot(selected_experts, num_experts), axis=-2)
    tokens_per_group_and_expert = jnp.mean(expert_mask.astype(jnp.float32), axis=-2)
    router_prob_per_group_and_expert = jnp.mean(routing_weights, axis=-1)
    return jnp.mean(tokens_per_group_and_expert * jnp.expand_dims(router_prob_per_group_and_expert, axis=-1)) * (
            num_experts ** 2)


class MixtralRMSNorm(nn.Module):
    dim: int
    eps: float = 1e-6
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16

    def setup(self) -> None:
        self.weight = self.param(
            'kernel',
            nn.initializers.ones,
            (self.dim,),
            self.param_dtype,
        )

    def _norm(self, x: jnp.ndarray) -> jnp.ndarray:
        return x * jax.lax.rsqrt(jnp.square(x).mean(-1, keepdims=True) + self.eps)

    def __call__(self, x: jnp.ndarray) -> jnp.ndarray:
        x = x.astype(jnp.promote_types(self.dtype, jnp.float32))
        output = self._norm(x).astype(self.dtype)
        weight = jnp.asarray(self.weight, self.dtype)
        return output * weight


class FlaxMixtralRotaryEmbedding(nn.Module):
    dtype: jnp.dtype = jnp.float32

    def __call__(self, key, query, freq_cis, position_ids):
        sin, cos = freq_cis

        sin = sin[position_ids][:, None, :, :]
        cos = cos[position_ids][:, None, :, :]

        key = apply_rotary_pos_emb(key, sin, cos)
        query = apply_rotary_pos_emb(query, sin, cos)

        return query.astype(self.dtype), key.astype(self.dtype)


class FlaxMixtralAttention(nn.Module):
    config: MixtralConfig
    layer_index: int
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        config = self.config
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings

        dense = functools.partial(
            nn.Dense,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
            kernel_init=nn.initializers.normal(),
            **get_dot_general_by_bits(self.config.bits, self.config.easy_method)
        )

        self.q_proj = dense(self.num_heads * self.head_dim)
        self.k_proj = dense(self.num_key_value_heads * self.head_dim)
        self.v_proj = dense(self.num_key_value_heads * self.head_dim)
        self.o_proj = dense(self.hidden_size)
        self.rotary = FlaxMixtralRotaryEmbedding(self.dtype)

    @nn.compact
    def concatenate_to_cache_(self, query: chex.Array, key: chex.Array, value: chex.Array, attention_mask: chex.Array):
        is_cache_available = self.has_variable('cache', 'key')
        key_cache = self.variable('cache', 'key', jnp.zeros, key.shape, key.dtype)
        value_cache = self.variable('cache', 'value', jnp.zeros, key.shape, value.dtype)
        index_cache = self.variable('cache', 'index', lambda: jnp.array(0, dtype=jnp.int32))
        if is_cache_available:
            *bd, ml, nh, dph = key_cache.value.shape
            indices = (0,) * len(bd) + (index_cache.value, 0, 0)
            key = jax.lax.dynamic_update_slice(key_cache.value, key, indices)
            value = jax.lax.dynamic_update_slice(value_cache.value, value, indices)
            key_cache.value = key
            value_cache.value = value
            num_updated_cache_vector = query.shape[1]
            index_cache.value = index_cache.value + num_updated_cache_vector
            pad_mask = jnp.broadcast_to(
                jnp.arange(ml) < index_cache.value,
                tuple(bd) + (1, num_updated_cache_vector, ml)
            )
            attention_mask = nn.combine_masks(pad_mask, attention_mask)
        return query, key, value, attention_mask

    @staticmethod
    def _t(query, key, value):
        return jnp.transpose(query, (0, 2, 1, 3)), jnp.transpose(key, (0, 2, 1, 3)), jnp.transpose(value, (0, 2, 1, 3))

    def t_rotary(self, batch_size, sequence_length, query, key, value, freq_cis, position_ids):
        query = query.reshape(batch_size, sequence_length, self.config.num_attention_heads, self.head_dim)
        key = key.reshape(batch_size, sequence_length, self.config.num_key_value_heads, self.head_dim)
        value = value.reshape(batch_size, sequence_length, self.config.num_key_value_heads, self.head_dim)

        query, key, value = self._t(query, key, value)
        query, key = self.rotary(position_ids=position_ids, query=query, key=key, freq_cis=freq_cis)
        key = repeat_kv_bnsh(key, self.num_key_value_groups)
        value = repeat_kv_bnsh(value, self.num_key_value_groups)
        return self._t(query, key, value)

    def __call__(
            self,
            hidden_states: chex.Array,
            freq_cis: chex.Array,
            attention_mask: chex.Array,
            causal_mask: chex.Array,
            position_ids: chex.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = True
    ):
        """
        The __call__ function is the main function of a JAX module.
        It defines how the module behaves when called as a function, and it's what you'll use to call your model in practice.
        The __call__ method takes an input tensor (x) and returns an output tensor (y).
        In this case, we're defining our model to be a simple linear layer with no activation: y = x @ w + b.

        :param self: Refer to the object itself
        :param hidden_states: chex.Array: Pass in the hidden state of the model
        :param freq_cis: chex.Array: Create the t_rotary variable
        :param attention_mask: chex.Array: Mask the attention weights
        :param causal_mask: chex.Array: Mask the attention weights
        :param position_ids: chex.Array: Specify the position of each token in a sequence
        :param deterministic: bool: Determine whether to use dropout or not
        :param init_cache: bool: Initialize the cache
        :param output_attentions: bool: Determine whether to return the attention weights
        :return: A tuple of (out, attn_output)

        """
        batch_size, sequence_length = hidden_states.shape[:2]
        query, key, value = self.q_proj(hidden_states), self.k_proj(hidden_states), self.v_proj(hidden_states)

        if self.config.use_pjit_attention_force:
            query = with_sharding_constraint(query, PS("fsdp", "sp", None))
            key = with_sharding_constraint(key, PS("fsdp", "sp", None))
            value = with_sharding_constraint(value, PS("fsdp", "sp", None))
        query, key, value = self.t_rotary(
            batch_size=batch_size,
            sequence_length=sequence_length,
            query=query,
            key=key,
            value=value,
            freq_cis=freq_cis,
            position_ids=position_ids
        )
        if self.has_variable('cache', 'key') or init_cache:
            query, key, value, attention_mask = self.concatenate_to_cache_(query, key, value, attention_mask)

        q_l, k_l = query.shape[1], key.shape[1]
        if self.has_variable('cache', 'key'):
            mask_shift: int = self.variables['cache']['index']
            dl = self.variables['cache']['key'].shape[1]
            causal_mask = jax.lax.dynamic_slice(
                causal_mask, (0, 0, mask_shift, 0), (1, 1, q_l, dl)
            )
        else:
            causal_mask = causal_mask[:, :, :q_l, :k_l]
        dropout_rng = None
        if not deterministic and self.config.attn_pdrop > 0.0:
            dropout_rng = self.make_rng("dropout")
        causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])
        if attention_mask.ndim == 2:
            attention_mask = jnp.broadcast_to(jnp.expand_dims(attention_mask, axis=(-3, -2)), causal_mask.shape)

        attention_mask = nn.combine_masks(attention_mask, causal_mask)

        if self.config.use_flash_attention and not (self.has_variable("cache", "cached_key") or init_cache):

            if attention_mask.ndim == 2:
                attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))

            if attention_mask.shape[1] != self.config.num_attention_heads:
                attention_mask = attention_mask.repeat(self.config.num_attention_heads, 1, )
            attention_bias = lax.select(
                attention_mask > 0,
                jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
                jnp.full(attention_mask.shape, jnp.finfo(self.dtype).min).astype(self.dtype),
            )
            attn_weights = None
            rtp_axis = (0, 2, 1, 3)
            attn_output = smart_flash_attention(
                q=jnp.transpose(query, rtp_axis),
                k=jnp.transpose(key, rtp_axis),
                v=jnp.transpose(value, rtp_axis),
                q_ps=self.config.q_ps,
                k_ps=self.config.k_ps,
                v_ps=self.config.v_ps,
                b_ps=self.config.b_ps,
                a_ps=self.config.a_ps,
                bias=attention_bias,
                block_q=self.config.flash_attn_query_chunk_size,
                block_k=self.config.flash_attn_key_chunk_size,
                block_b=1,
                num_attention_heads=self.config.num_attention_heads,
                precision=self.precision,
                dtype=self.dtype,
                causal=False,
                mesh=self.config.jax_mesh(),
                dropout_rng=dropout_rng,
                deterministic=deterministic,
                q_seq_len=q_l,
                kv_seq_len=k_l,
                attn_pdrop=self.config.attn_pdrop,
                head_dims=self.head_dim,
                force_float32_tpu=True
            )
            attn_output = jnp.transpose(attn_output, rtp_axis)
        else:
            attention_bias = lax.select(
                attention_mask > 0,
                jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
                jnp.full(attention_mask.shape, jnp.finfo(self.dtype).min).astype(self.dtype),
            )
            if self.config.use_shard_map:
                attn_weights = shard_map(
                    functools.partial(
                        dot_product_attention_weights,
                        dtype=jnp.promote_types(self.dtype, jnp.float32),
                        deterministic=deterministic,
                        dropout_rate=self.config.attn_pdrop,
                        precision=self.precision,
                    ),
                    mesh=self.config.jax_mesh(),
                    in_specs=(
                        self.config.q_ps,
                        self.config.k_ps,
                        self.config.b_ps
                    ),
                    out_specs=PS(("dp", "fsdp"), "sp", "tp", None),
                    check_rep=False
                )(
                    query, key, attention_bias
                )
            else:
                attn_weights = dot_product_attention_weights(
                    query=query,
                    key=key,
                    bias=attention_bias,
                    dtype=jnp.promote_types(self.dtype, jnp.float32),
                    deterministic=deterministic,
                    dropout_rate=self.config.attn_pdrop,
                    precision=self.precision,
                )

            if self.config.use_pjit_attention_force:
                attn_weights = with_sharding_constraint(attn_weights, PS(("dp", "fsdp"), "sp", "tp", None))

            attn_output = jnp.einsum("...hqk,...khd->...qhd", attn_weights, value)

        out = self.o_proj(attn_output.reshape(batch_size, sequence_length, self.hidden_size))
        return out, attn_weights


class FlaxMixtralBLockSparseTop2MLP(nn.Module):
    config: MixtralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        dense = functools.partial(
            nn.Dense,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
            kernel_init=nn.initializers.normal(),
            **get_dot_general_by_bits(self.config.bits, self.config.easy_method)
        )
        self.w1 = dense(self.config.intermediate_size)
        self.w3 = dense(self.config.intermediate_size)
        self.w2 = dense(self.config.hidden_size)
        self.act_fn = ACT2FN[self.config.hidden_act]

    def __call__(self, x: chex.Array):
        return self.w2(self.act_fn(self.w1(x)) * self.w3(x))


class FlaxMixtralBlocKSparesTop2MLPCollection(nn.Module):
    config: MixtralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        self.layers = [
            FlaxMixtralBLockSparseTop2MLP(
                config=self.config,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                precision=self.precision,
                name=str(i)
            )
            for i in range(self.config.num_local_experts)
        ]

    def __call__(self,
                 expert_mask: chex.Array,
                 hidden_states: chex.Array,
                 routing_weights: chex.Array,
                 batch_size: int,
                 sequence_length: int,
                 hidden_dim: int
                 ) -> chex.Array:
        assert hidden_states.ndim == 2
        final_hidden_states = jnp.zeros(
            (batch_size * sequence_length, hidden_dim), dtype=hidden_states.dtype
        )

        def custom_index_add_without_index_add(
                final_hidden_states_,
                top_x_,
                idx_,
                current_hidden_states_
        ):
            for i in range(top_x_.size):
                # if (idx_[i]):
                final_hidden_states_.at[top_x[i]].set(final_hidden_states_[top_x[i]] + current_hidden_states_[i])
            return final_hidden_states_

        for expert_idx, expert_layer in enumerate(self.layers):
            selected_mask = expert_mask[expert_idx]

            idx, top_x = jnp.nonzero(selected_mask, size=selected_mask.shape[-1])
            top_x = jnp.where(top_x != 0, top_x, -1)
            if top_x.shape[0] == 0:
                continue

            current_state = hidden_states[None, top_x].reshape(-1, hidden_dim)

            current_hidden_states = expert_layer(current_state) * routing_weights[top_x, idx, None]

            final_hidden_states = custom_index_add_without_index_add(
                final_hidden_states,
                top_x,
                idx,
                current_hidden_states.astype(hidden_states.dtype)
            )

        return final_hidden_states.reshape(batch_size, sequence_length, hidden_dim)


class FlaxMixtralSparseMoeBlock(nn.Module):
    """
    This implementation is
    strictly equivalent to standard MoE with full capacity (no
    dropped tokens). It's faster since it formulates MoE operations
    in terms of block-sparse operations to accomodate imbalanced
    assignments of tokens to experts, whereas standard MoE either
    (1) drop tokens at the cost of reduced performance or (2) set
    capacity factor to number of experts and thus waste computation
    and memory on padding.
    """
    config: MixtralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        self.gate = nn.Dense(
            self.config.num_local_experts,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
            kernel_init=nn.initializers.normal(),
        )

        self.experts = FlaxMixtralBlocKSparesTop2MLPCollection(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

    def __call__(self, hidden_states: chex.Array) -> Tuple[chex.Array, chex.Array]:
        batch_size, sequence_length, hidden_dim = hidden_states.shape
        hidden_states = hidden_states.reshape(-1, hidden_dim)
        router_logits = self.gate(hidden_states).astype(jnp.promote_types(self.dtype, jnp.float32))
        routing_weights = jax.nn.softmax(router_logits.astype(jnp.promote_types(self.dtype, jnp.float32)), axis=1)
        routing_weights, selected_experts = jax.lax.top_k(routing_weights, k=self.config.num_experts_per_tok)
        routing_weights /= jnp.sum(routing_weights, axis=-1, keepdims=True)
        routing_weights = routing_weights.astype(hidden_states.dtype)
        expert_mask = jax.nn.one_hot(selected_experts, num_classes=self.config.num_local_experts).transpose(2, 1, 0)
        return self.experts(
            expert_mask=expert_mask,
            batch_size=batch_size,
            sequence_length=sequence_length,
            hidden_dim=hidden_dim,
            hidden_states=hidden_states,
            routing_weights=routing_weights
        ), router_logits


class FlaxMixtralDecoderLayer(nn.Module):
    config: MixtralConfig
    layer_index: int
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        self.self_attn = FlaxMixtralAttention(
            config=self.config,
            layer_index=self.layer_index,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.block_sparse_moe = FlaxMixtralSparseMoeBlock(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.input_layernorm = MixtralRMSNorm(
            dim=self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )
        self.post_attention_layernorm = MixtralRMSNorm(
            dim=self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )

    def __call__(
            self,
            hidden_states: chex.Array,
            freq_cis: chex.Array,
            attention_mask: chex.Array,
            causal_mask: chex.Array,
            position_ids: chex.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = True,
            output_router_logits: Optional[bool] = False,
    ):
        """
        The __call__ function is the main function of a TransformerEncoderLayer.
        It takes in the following arguments:
            hidden_states (chex.Array): The input to the encoder layer, which is also its output after being processed by all sublayers.
            freq_cis (chex.Array): A tensor containing frequency-domain representations of each token's context vector, used for computing self-attention weights and biases in a more efficient manner than using position embeddings or sinusoidal positional encoding vectors would allow for [2]. This tensor has shape `(batch_size, num

        :param self: Represent the instance of the class
        :param hidden_states: chex.Array: Represent the input to the encoder layer
        :param freq_cis: chex.Array: Pass the frequency information to the attention layer
        :param attention_mask: chex.Array: Mask out the attention weights for certain positions
        :param causal_mask: chex.Array: Mask the future tokens
        :param position_ids: chex.Array: Indicate the position of each token in the sequence
        :param deterministic: bool: Determine whether to use dropout or not
        :param init_cache: bool: Initialize the cache for the self-attention layer
        :param output_attentions: bool: Determine whether to return the attention weights or not
        :return: A tuple of hidden_states and attention_output

        """
        residual = hidden_states
        hidden_states = self.input_layernorm(hidden_states)
        hidden_states, self_attn_weights = self.self_attn(
            hidden_states=hidden_states,
            freq_cis=freq_cis,
            attention_mask=attention_mask,
            causal_mask=causal_mask,
            position_ids=position_ids,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions
        )

        hidden_states = residual + hidden_states

        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states, router_logits = self.block_sparse_moe(hidden_states)
        hidden_states = residual + hidden_states

        outputs = (hidden_states,)
        if output_attentions:
            outputs += (self_attn_weights,)
        if output_router_logits:
            outputs += (router_logits,)
        return outputs


class FlaxMixtralDecoderLayerCollection(nn.Module):
    config: MixtralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    def setup(self) -> None:
        self.blocks = [
            FlaxMixtralDecoderLayer(
                layer_index=layer_index,
                config=self.config,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                precision=self.precision,
                name=str(layer_index)
            )

            for layer_index in range(self.config.num_hidden_layers)
        ]

    def __call__(
            self,
            hidden_states: chex.Array,
            freq_cis: chex.Array,
            attention_mask: chex.Array,
            causal_mask: chex.Array,
            position_ids: chex.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_hidden_states: Optional[bool] = False,
            output_attentions: Optional[bool] = False,
            output_router_logits: Optional[bool] = False,
    ):
        """
        The __call__ function is the main function of a TransformerEncoderLayer.
        It takes in the following arguments:
            hidden_states (chex.Array): The input to the encoder layer, which is also its output after being processed by all sublayers.
            freq_cis (chex.Array): A tensor containing frequency-domain representations of each token's context vector, used for computing self-attention weights and biases in a more efficient manner than using position embeddings or sinusoidal positional encoding vectors would allow for [2]. This tensor has shape `(batch_size, num

        :param self: Represent the instance of the class
        :param hidden_states: chex.Array: Represent the input to the encoder layer
        :param freq_cis: chex.Array: Pass the frequency information to the attention layer
        :param attention_mask: chex.Array: Mask out the attention weights for certain positions
        :param causal_mask: chex.Array: Mask the future tokens
        :param position_ids: chex.Array: Indicate the position of each token in the sequence
        :param deterministic: bool: Determine whether to use dropout or not
        :param init_cache: bool: Initialize the cache for the self-attention layer
        :param output_attentions: bool: Determine whether to return the attention weights or not
        :return: A tuple of hidden_states, attention_output, all_hidden_states and all_router_logits

        """
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        all_router_logits = () if output_router_logits else None

        for block in self.blocks:
            if output_hidden_states:
                all_hidden_states += (hidden_states,)
            layer_outputs = block(
                hidden_states=hidden_states,
                attention_mask=attention_mask,
                position_ids=position_ids,
                output_attentions=output_attentions,
                output_router_logits=output_router_logits,
                init_cache=init_cache,
                freq_cis=freq_cis,
                causal_mask=causal_mask,
                deterministic=deterministic,
            )

            hidden_states = layer_outputs[0]

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

            if output_router_logits:
                all_router_logits += (layer_outputs[-1],)

        outputs = (hidden_states,)
        if output_attentions:
            outputs += (all_self_attns,)
        if output_hidden_states:
            outputs += (all_hidden_states,)
        if output_router_logits:
            outputs += (all_router_logits,)
        return outputs


class MixtralPreTrainedModel(FlaxPreTrainedModel):
    config_class: MixtralConfig = MixtralConfig
    module_class: nn.Module = None
    base_model_prefix = "model"

    # main_input_name = "input_ids"

    def __init__(
            self,
            config: MixtralConfig,
            dtype: jnp.dtype = jnp.bfloat16,
            param_dtype: jnp.dtype = jnp.bfloat16,
            precision: jax.lax.Precision = jax.lax.Precision("fastest"),
            input_shape: Tuple[int, int] = (1, 1),
            seed: int = 0,
            _do_init: bool = False,
            **kwargs
    ):
        module = self.module_class(
            config=config,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision,
            **kwargs
        )

        super().__init__(
            dtype=dtype, _do_init=_do_init,
            module=module, config=config, input_shape=input_shape,
            seed=seed,
        )

    def init_weights(
            self,
            rng: jax.random.PRNGKey,
            input_shape: Tuple,
            params: FrozenDict = None
    ) -> FrozenDict:
        """
        The init_weights function is used to initialize the weights of a model.
        It takes in a rng, which is a random number generator key that can be used to generate random numbers.
        The input_shape parameter specifies the shape of the inputs that will be fed into this model.
        The params parameter allows you to pass in pre-trained weights for your model, if you have them available.

        :param self: Access variables that belong to the class
        :param rng: jax.random.PRNGKey: Initialize the weights of the model
        :param input_shape: Tuple: Initialize the input_ids, attention_mask and position_ids
        :param params: flax.core.FrozenDict: Pass in the parameters of a pre-trained model
        :return: A frozendict of parameters
        """
        input_ids = jnp.zeros(input_shape, dtype="i4")
        attention_mask = jnp.ones_like(input_ids, dtype="i4")
        position_ids = jnp.broadcast_to(
            jnp.arange(jnp.atleast_2d(input_ids).shape[-1], dtype="i4"),
            input_shape,
        )
        params_rng, dropout_rng = jax.random.split(rng)
        rngs = {"params": params_rng, "dropout": dropout_rng}
        if self.config.add_cross_attention:
            encoder_hidden_states = jnp.zeros(input_shape + (self.config.hidden_size,))
            encoder_attention_mask = attention_mask
            module_init_outputs = self.module.init(
                rngs,
                input_ids,
                attention_mask,
                position_ids,
                encoder_hidden_states,
                encoder_attention_mask,
                return_dict=False,
            )
        else:
            module_init_outputs = self.module.init(
                rngs,
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                return_dict=False
            )
        random_params = module_init_outputs["params"]

        if params is not None:
            random_params = flatten_dict(unfreeze(random_params))
            params = flatten_dict(unfreeze(params))
            for missing_key in self._missing_keys:
                params[missing_key] = random_params[missing_key]
            self._missing_keys = set()
            return freeze(unflatten_dict(params))
        else:
            return random_params

    def init_cache(self, batch_size, max_length):

        input_ids = jnp.ones((batch_size, max_length))
        attention_mask = jnp.ones_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_ids.shape)

        init_variables = self.module.init(
            jax.random.PRNGKey(0), input_ids, attention_mask, position_ids, return_dict=False, init_cache=True
        )
        return init_variables["cache"]

    def __call__(
            self,
            input_ids: chex.Array,
            attention_mask: Optional[chex.Array] = None,
            position_ids: Optional[chex.Array] = None,
            params: dict = None,
            past_key_values: dict = None,
            dropout_rng: jax.random.PRNGKey = None,
            train: bool = False,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            output_router_logits: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            add_params_field: bool = False
    ):
        """
        The __call__ function is the main function of a JAX module.
        It takes as input:
        - The parameters of the model (self.params)
        - The inputs to the model (input_ids, attention_mask, position_ids)
        - Whether we are training (train=True/False) and whether we want to return all hidden states and
        attentions weights at each layer in addition to just the last layer output (output_hidden_states=True/False).

        :param self: Represent the instance of the class
        :param input_ids: Pass the input sequence to the model
        :param attention_mask: Mask out the padding tokens
        :param position_ids: Specify the position of each token in the sequence
        :param params: dict: Pass in the parameters of the model
        :param past_key_values: dict: Pass the past key values to the model
        :param dropout_rng: jax.random.PRNGKey: Pass in a random number generator key to the model
        :param train: bool: Determine whether to use dropout or not
        :param output_attentions: Optional[bool]: Determine whether to return the attention weights
        :param output_hidden_states: Optional[bool]: Determine whether to return the hidden states of all layers
        :param return_dict: Optional[bool]: Return a dictionary of the outputs
        :param add_params_field: bool: Add a params field to the inputs dictionary
        :return: A tuple of (last_hidden_state, past_key_values)

        """

        # TODO: Here needs to be fixed
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        batch_size, sequence_length = input_ids.shape

        if position_ids is None:
            if past_key_values is not None:
                raise ValueError("Make sure to provide `position_ids` when passing `past_key_values`.")

            position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

        if attention_mask is None:
            attention_mask = jnp.ones((batch_size, sequence_length))

        rng_s = {}
        if dropout_rng is not None:
            rng_s["dropout"] = dropout_rng

        inputs = {"params": params or self.params} if add_params_field else params or self.params

        if self.config.bits is not None:
            rng_s['params'] = jax.random.key(0)
        if past_key_values:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False

        outputs = self.module.apply(
            inputs,
            jnp.array(input_ids, dtype="i4"),  # input_ids: chex.Array
            jnp.array(attention_mask, dtype="i4"),  # attention_mask: Optional[chex.Array] = None
            jnp.array(position_ids, dtype="i4"),  # position_ids: Optional[chex.Array] = None
            None,  # inputs_embeds: Optional[chex.Array] = None
            output_attentions,  # output_attentions: Optional[bool] = None
            output_hidden_states,  # output_hidden_states: Optional[bool] = None
            output_router_logits,  # output_router_logits: Optional[bool] = None
            False,  # init_cache: bool = False
            not train,  # deterministic: bool = True
            return_dict,  # return_dict: bool = True
            rngs=rng_s,
            mutable=mutable,
        )

        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs


class FlaxMixtralModule(nn.Module):
    config: MixtralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    def setup(self) -> None:
        self.embed_tokens = nn.Embed(
            self.config.vocab_size,
            self.config.hidden_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )

        self.layers = FlaxMixtralDecoderLayerCollection(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

        self.norm = MixtralRMSNorm(
            self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )

        self.freq_cis = precompute_freq_cis(
            max_position_embedding=self.config.freq_max_position_embeddings if self.config.freq_max_position_embeddings is not None else self.config.max_position_embeddings,
            head_dim=self.config.hidden_size // self.config.num_attention_heads
        )
        self.causal_mask = nn.make_causal_mask(jnp.ones((1, self.config.c_max_position_embeddings), dtype='i4'))

    def __call__(
            self,
            input_ids: chex.Array,
            attention_mask: chex.Array,
            position_ids: chex.Array,
            inputs_embeds: Optional[chex.Array] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            output_router_logits: Optional[bool] = None,
            init_cache: bool = False,
            deterministic: bool = True,
            return_dict: bool = True,
    ) -> MoeModelOutput | Tuple:
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both decoder_input_ids and decoder_inputs_embeds at the same time")

        if inputs_embeds is None and input_ids is not None:
            inputs_embeds = self.embed_tokens(input_ids.astype("i4"))
        else:
            raise ValueError("you should specify inputs_embeds or input_ids one of them")
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_router_logits = (
            output_router_logits if output_router_logits is not None else self.config.output_router_logits
        )
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        collection_outputs = self.layers(
            hidden_states=inputs_embeds,
            attention_mask=attention_mask,
            position_ids=position_ids,
            causal_mask=self.causal_mask,
            freq_cis=self.freq_cis,
            output_attentions=output_attentions,
            output_router_logits=output_router_logits,
            output_hidden_states=output_hidden_states,
            init_cache=init_cache,
            deterministic=deterministic,
        )
        all_self_attns = None
        all_hidden_states = None
        all_router_logits = None
        hidden_states = collection_outputs[0]
        if output_attentions:
            all_self_attns = collection_outputs[1]
        if output_hidden_states:
            all_hidden_states = collection_outputs[2 if output_attentions else 1]
        if output_router_logits:
            all_router_logits = collection_outputs[-1]
        hidden_states = self.norm(hidden_states)

        if output_hidden_states:
            all_hidden_states += (hidden_states,)
        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, all_hidden_states, all_self_attns, all_router_logits]
                if v is not None
            )
        return MoeModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
            router_logits=all_router_logits,
        )


class FlaxMixtralModel(MixtralPreTrainedModel):
    module_class = FlaxMixtralModule


class FlaxMixtralForCausalLMModule(nn.Module):
    config: MixtralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    def setup(self) -> None:
        self.model = FlaxMixtralModule(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.lm_head = nn.Dense(
            self.config.vocab_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
            use_bias=False,
            kernel_init=nn.initializers.normal(self.config.initializer_range),
            **get_dot_general_by_bits(self.config.bits, self.config.easy_method)
        )

    def __call__(
            self,
            input_ids: chex.Array,
            attention_mask: Optional[chex.Array] = None,
            position_ids: Optional[chex.Array] = None,
            inputs_embeds: Optional[chex.Array] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            output_router_logits: Optional[bool] = None,
            init_cache: bool = False,
            deterministic: bool = True,
            return_dict: bool = True,
    ) -> MoeCausalLMOutput | Tuple:
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            output_router_logits=output_router_logits,
            init_cache=init_cache,
            deterministic=deterministic,
            return_dict=True,
        )
        logits = self.lm_head(outputs.last_hidden_state)
        aux_loss = None
        if output_router_logits and outputs.router_logits is not None:
            aux_loss = jax_load_balancing_loss_func(
                outputs.router_logits, self.num_experts, self.num_experts_per_tok
            )

        if not return_dict:
            outputs = (logits,) + tuple(
                v
                for v in [
                    outputs.hidden_states,
                    outputs.attentions,
                    outputs.router_logits
                ]
                if v is not None
            )
            return outputs

        return MoeCausalLMOutput(
            aux_loss=aux_loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            router_logits=outputs.router_logits,
        )


class FlaxMixtralForCausalLM(MixtralPreTrainedModel):
    module_class = FlaxMixtralForCausalLMModule

    def prepare_inputs_for_generation(self, input_ids, max_length, attention_mask: Optional[chex.Array] = None):
        """
        The prepare_inputs_for_generation function is used to prepare the inputs for a generation task.

        :param self: Access variables that belong to the class
        :param input_ids: Pass in the input tokens
        :param max_length: Set the length of the sequence to be generated
        :param attention_mask: Optional[chex.Array]: Mask the attention weights
        :return: A dictionary of the past_key_values, attention_mask and position ids

        """
        batch_size, seq_length = input_ids.shape

        past_key_values = self.init_cache(batch_size, max_length)
        extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
        if attention_mask is not None:
            position_ids = attention_mask.cumsum(axis=-1) - 1
            extended_attention_mask = lax.dynamic_update_slice(extended_attention_mask, attention_mask, (0, 0))
        else:
            position_ids = jnp.broadcast_to(jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length))

        return {
            "past_key_values": past_key_values,
            "attention_mask": extended_attention_mask,
            "position_ids": position_ids,
        }

    def update_inputs_for_generation(self, model_outputs, model_kwargs):
        model_kwargs["past_key_values"] = model_outputs.past_key_values
        model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
        return model_kwargs
