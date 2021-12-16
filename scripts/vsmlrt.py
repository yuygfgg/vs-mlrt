__version__ = "3.0.0"

from dataclasses import dataclass
import enum
import math
import os.path
import typing

import vapoursynth as vs
from vapoursynth import core


class Backend:
    @dataclass(frozen=True)
    class ORT_CPU:
        num_streams: int = 1
        verbosity: int = 2

    @dataclass(frozen=True)
    class ORT_CUDA:
        device_id: int = 0
        cudnn_benchmark: bool = True
        num_streams: int = 1
        verbosity: int = 2

    @dataclass(frozen=True)
    class OV_CPU:
        pass


def calcSize(width: int, tiles: int, overlap: int, multiple: int = 1) -> int:
    return math.ceil((width + 2 * overlap * (tiles - 1)) / (tiles * multiple)) * multiple


def inference(
    clips: typing.List[vs.VideoNode],
    network_path: str,
    overlap: typing.Tuple[int, int],
    tilesize: typing.Tuple[int, int],
    backend: typing.Union[Backend.OV_CPU, Backend.ORT_CPU, Backend.ORT_CUDA]
) -> vs.VideoNode:

    if isinstance(backend, Backend.ORT_CPU):
        return core.ort.Model(
            clips, network_path,
            overlap=overlap, tilesize=tilesize,
            provider="CPU", builtin=1,
            num_streams=backend.num_streams,
            verbosity=backend.verbosity
        )
    elif isinstance(backend, Backend.ORT_CUDA):
        return core.ort.Model(
            clips, network_path,
            overlap=overlap, tilesize=tilesize,
            provider="CUDA", builtin=1,
            device_id=backend.device_id,
            num_streams=backend.num_streams,
            verbosity=backend.verbosity,
            cudnn_benchmark=backend.cudnn_benchmark
        )
    elif isinstance(backend, Backend.OV_CPU):
        return core.ov.Model(
            clips, network_path,
            overlap=overlap, tilesize=tilesize,
            device="CPU", builtin=1
        )
    else:
        raise ValueError(f'unknown backend {backend}')


@enum.unique
class Waifu2xModel(enum.IntEnum):
    anime_style_art = 0
    anime_style_art_rgb = 1
    photo = 2
    upconv_7_anime_style_art_rgb = 3
    upconv_7_photo = 4
    upresnet10 = 5
    cunet = 6


def Waifu2x(
    clip: vs.VideoNode,
    noise: typing.Literal[-1, 0, 1, 2, 3] = -1,
    scale: typing.Literal[1, 2] = 2,
    tiles: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    tilesize: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    overlap: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    model: typing.Literal[0, 1, 2, 3, 4, 5, 6] = 6,
    backend: typing.Union[Backend.OV_CPU, Backend.ORT_CPU, Backend.ORT_CUDA] = Backend.OV_CPU()
) -> vs.VideoNode:

    funcName = "vsmlrt.Waifu2x"

    if not isinstance(clip, vs.VideoNode):
        raise TypeError(f'{funcName}: "clip" must be a clip!')

    if clip.format.sample_type != vs.FLOAT or clip.format.bits_per_sample != 32:
        raise ValueError(f"{funcName}: only constant format 32 bit float input supported")

    if not isinstance(noise, int) or noise not in range(-1, 4):
        raise ValueError(f'{funcName}: "noise" must be -1, 0, 1, 2, or 3')

    if not isinstance(scale, int) or scale not in (1, 2):
        raise ValueError(f'{funcName}: "scale" must be 1 or 2')

    if not isinstance(model, int) or model not in Waifu2xModel.__members__.values():
        raise ValueError(f'{funcName}: "model" must be 0, 1, 2, 3, 4, 5, or 6')

    if model == 0 and noise == 0:
        raise ValueError(
            f'{funcName}: "anime_style_art" model'
            ' does not support noise reduction level 0'
        )

    if model == 0:
        if clip.format.id != vs.GRAYS:
            raise ValueError(f'{funcName}: "clip" must be of GRAYS format')
    elif clip.format.id != vs.RGBS:
        raise ValueError(f'{funcName}: "clip" must be of RGBS format')

    if overlap is None:
        overlap_w = overlap_h = [8, 8, 8, 8, 8, 4, 4][model]
    elif isinstance(overlap, int):
        overlap_w = overlap_h = overlap
    else:
        overlap_w, overlap_h = overlap

    if model == 6:
        multiple = 4
    else:
        multiple = 1

    if tilesize is None:
        if tiles is None:
            overlap = 0
            tile_w = clip.width
            tile_h = clip.height
        elif isinstance(tiles, int):
            tile_w = calcSize(clip.width, tiles, overlap_w, multiple)
            tile_h = calcSize(clip.height, tiles, overlap_h, multiple)
        else:
            tile_w = calcSize(clip.width, tiles[0], overlap_w, multiple)
            tile_h = calcSize(clip.height, tiles[1], overlap_h, multiple)
    elif isinstance(tilesize, int):
        tile_w = tilesize
        tile_h = tilesize
    else:
        tile_w, tile_h = tilesize

    if model == 6 and (tile_w % 4 != 0 or tile_h % 4 != 0):
        raise ValueError(f'{funcName}: tile size of cunet model must be divisible by 4 ({tile_w}, {tile_h})')

    if backend is Backend.ORT_CPU: # type: ignore
        backend = Backend.ORT_CPU()
    elif backend is Backend.ORT_CUDA: # type: ignore
        backend = Backend.ORT_CUDA()
    elif backend is Backend.OV_CPU: # type: ignore
        backend = Backend.OV_CPU()

    folder_path = os.path.join("waifu2x", tuple(Waifu2xModel.__members__)[model])

    if model in (0, 1, 2):
        if noise == -1:
            model_name = "scale2.0x_model.onnx"
        else:
             model_name = f"noise{noise}_model.onnx"
    elif model in (3, 4, 5):
        if noise == -1:
            model_name = "scale2.0x_model.onnx"
        else:
            model_name = f"noise{noise}_scale2.0x_model.onnx"
    else:
        if scale == 1:
            scale_name = ""
        else:
            scale_name = "scale2.0x_"

        if noise == -1:
            model_name = "scale2.0x_model.onnx"
        else:
            model_name = f"noise{noise}_{scale_name}model.onnx"

    network_path = os.path.join(folder_path, model_name)

    width, height = clip.width, clip.height
    if model in (0, 1, 2):
        # emulating cv2.resize(interpolation=cv2.INTER_CUBIC)
        clip = core.resize.Bicubic(
            clip,
            width * 2, height * 2,
            filter_param_a=0, filter_param_b=0.75
        )

    clip = inference(
        clips=[clip], network_path=network_path,
        overlap=(overlap_w, overlap_h), tilesize=(tile_w, tile_h),
        backend=backend
    )

    if scale == 1 and clip.width // width == 2:
        # emulating cv2.resize(interpolation=cv2.INTER_CUBIC)
        # cr: @AkarinVS
        clip = core.fmtc.resample(
            clip, scale=0.5,
            kernel="impulse", impulse=[-0.1875, 1.375, -0.1875],
            kovrspl=2
        )

    return clip


@enum.unique
class DPIRModel(enum.IntEnum):
    drunet_gray = 0
    drunet_color = 1
    drunet_deblocking_grayscale = 2
    drunet_deblocking_color = 3


def DPIR(
    clip: vs.VideoNode,
    strength: typing.Optional[typing.Union[typing.SupportsFloat, vs.VideoNode]],
    tiles: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    tilesize: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    overlap: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    model: typing.Literal[0, 1, 2, 3] = 0,
    backend: typing.Union[Backend.OV_CPU, Backend.ORT_CPU, Backend.ORT_CUDA] = Backend.OV_CPU()
) -> vs.VideoNode:

    funcName = "vsmlrt.DPIR"

    if not isinstance(clip, vs.VideoNode):
        raise TypeError(f'{funcName}: "clip" must be a clip!')

    if clip.format.sample_type != vs.FLOAT or clip.format.bits_per_sample != 32:
        raise ValueError(f"{funcName}: only constant format 32 bit float input supported")

    if not isinstance(model, int) or model not in DPIRModel.__members__.values():
        raise ValueError(f'{funcName}: "model" must be 0, 1, 2 or 3')

    if model in [0, 2] and clip.format.id != vs.GRAYS:
        raise ValueError(f'{funcName}: "clip" must be of GRAYS format')
    elif model in [1, 3] and clip.format.id != vs.RGBS:
        raise ValueError(f'{funcName}: "clip" must be of RGBS format')

    if strength is None:
        strength = 5.0

    if isinstance(strength, vs.VideoNode):
        if strength.format.id != vs.GRAYS:
            raise ValueError(f'{funcName}: "strength" must be of GRAYS format')
        if strength.width != clip.width or strength.height != clip.height:
            raise ValueError(f'{funcName}: "strength" must be of the same size as "clip"')
        if strength.num_frames != clip.num_frames:
            raise ValueError(f'{funcName}: "strength" must be of the same length as "clip"')

        strength = core.std.Expr(strength, f"x 255 /")
    else:
        try:
            strength = float(strength)
        except TypeError:
            raise TypeError(f'{funcName}: "strength" must be a float or a clip')

        strength = core.std.BlankClip(clip, format=vs.GRAYS, color=strength / 255)

    if overlap is None:
        overlap_w = overlap_h = 0
    elif isinstance(overlap, int):
        overlap_w = overlap_h = overlap
    else:
        overlap_w, overlap_h = overlap

    multiple = 8

    if tilesize is None:
        if tiles is None:
            overlap = 0
            tile_w = clip.width
            tile_h = clip.height
        elif isinstance(tiles, int):
            tile_w = calcSize(clip.width, tiles, overlap_w, multiple)
            tile_h = calcSize(clip.height, tiles, overlap_h, multiple)
        else:
            tile_w = calcSize(clip.width, tiles[0], overlap_w, multiple)
            tile_h = calcSize(clip.height, tiles[1], overlap_h, multiple)
    elif isinstance(tilesize, int):
        tile_w = tilesize
        tile_h = tilesize
    else:
        tile_w, tile_h = tilesize

    if tile_w % 8 != 0 or tile_h % 8 != 0:
        raise ValueError(f'{funcName}: tile size must be divisible by 8 ({tile_w}, {tile_h})')

    if backend is Backend.ORT_CPU: # type: ignore
        backend = Backend.ORT_CPU()
    elif backend is Backend.ORT_CUDA: # type: ignore
        backend = Backend.ORT_CUDA()
    elif backend is Backend.OV_CPU: # type: ignore
        backend = Backend.OV_CPU()

    network_path = os.path.join("dpir", f"{tuple(DPIRModel.__members__)[model]}.onnx")

    clip = inference(
        clips=[clip, strength], network_path=network_path,
        overlap=(overlap_w, overlap_h), tilesize=(tile_w, tile_h),
        backend=backend
    )

    return clip