from __future__ import annotations

import re
import struct
from pathlib import Path

try:
    from typing import BinaryIO, Self, Sequence, TextIO
except ImportError:
    from typing_extensions import Self

import lzf
import numpy as np
from pydantic import BaseModel, validator
from pydantic.dataclasses import dataclass

NUMPY_TYPE_TO_PCD_TYPE: dict[
    np.dtype,
    tuple[str, int],
] = {
    np.dtype("uint8"): ("U", 1),
    np.dtype("uint16"): ("U", 2),
    np.dtype("uint32"): ("U", 4),
    np.dtype("uint64"): ("U", 8),
    np.dtype("int8"): ("I", 1),
    np.dtype("int16"): ("I", 2),
    np.dtype("int32"): ("I", 4),
    np.dtype("int64"): ("I", 8),
    np.dtype("float32"): ("F", 4),
    np.dtype("float64"): ("F", 8),
}

PCD_TYPE_TO_NUMPY_TYPE: dict[
    tuple[str, int],
    type[np.floating | np.integer],
] = {
    ("F", 4): np.float32,
    ("F", 8): np.float64,
    ("U", 1): np.uint8,
    ("U", 2): np.uint16,
    ("U", 4): np.uint32,
    ("U", 8): np.uint64,
    ("I", 1): np.int8,
    ("I", 2): np.int16,
    ("I", 4): np.int32,
    ("I", 8): np.int64,
}

HEADER_PATTERN = re.compile(r"(\w+)\s+([\w\s\.]+)")


@dataclass
class MetaData(BaseModel):
    fields: Sequence[str]
    size: Sequence[int]
    type: Sequence[str]
    count: Sequence[int]
    points: int
    width: int
    height: int = 1
    version: str = "0.7"
    viewpoint: Sequence[float] = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    data: str = "binary_compressed"

    @validator("version")
    def validate_version(cls: Self, value: str) -> str:
        if value not in ["0.7"]:
            raise RuntimeError("pypcd4 supports PCD file formats version 0.7 only.")

        return value

    @validator("points")
    def validate_points(cls: Self, value: int) -> int:
        if value <= 0:
            raise RuntimeError(f"Number of points must be greater than zero, but got {value}")

        return value

    @validator("data")
    def validate_data(cls: Self, value: str) -> str:
        value = "binary_compressed" if value == "binaryscompressed" else value

        if value not in ["ascii", "binary", "binary_compressed"]:
            raise RuntimeError(
                f"Got unsupported data type: {value}. As of version 0.7, "
                "three data types are supported: ascii, binary, and binary_compressed. "
            )

        return value

    @staticmethod
    def parse_header(lines: list[str]) -> MetaData:
        """
        Parse a header in accordance with the PCD (Point Cloud Data) file format.
        See https://pointclouds.org/documentation/tutorials/pcd_file_format.html.
        """

        _header = {}
        for line in lines:
            if line.startswith("#") or len(line) < 2:
                continue

            line = line.replace("_", "s", 1)
            line = line.replace("_", "m", 1)

            if (match := re.match(HEADER_PATTERN, line)) is None:
                continue

            value = match.group(2).split()
            if (key := match.group(1).lower()) in ["version", "data"]:
                _header[key] = value[0]
            elif key in ["width", "height", "points"]:
                _header[key] = int(value[0])
            elif key in ["fields", "type"]:
                _header[key] = value
            elif key in ["size", "count"]:
                _header[key] = [int(v) for v in value]
            elif key in ["viewpoint"]:
                _header[key] = [float(v) for v in value]

        return MetaData.model_validate(_header)

    def compose_header(self) -> str:
        header: list[str] = []

        header.append(f"VERSION {self.version}")
        header.append(f"FIELDS {' '.join(self.fields)}")
        header.append(f"SIZE {' '.join([str(v) for v in self.size])}")
        header.append(f"TYPE {' '.join(self.type)}")
        header.append(f"COUNT {' '.join([str(v) for v in self.count])}")
        header.append(f"WIDTH {self.width}")
        header.append(f"HEIGHT {self.height}")
        header.append(f"VIEWPOINT {' '.join([str(v) for v in self.viewpoint])}")
        header.append(f"POINTS {self.points}")
        header.append(f"DATA {self.data}")

        return "\n".join(header) + "\n"

    def build_dtype(self) -> np.dtype[np.void]:
        field_names: list[str] = []
        type_names: list[np.dtype] = []

        for i, field in enumerate(self.fields):
            np_type: np.dtype = np.dtype(PCD_TYPE_TO_NUMPY_TYPE[(self.type[i], self.size[i])])

            if (count := self.count[i]) == 1:
                field_names.append(self.fields[i])
                type_names.append(np_type)
            else:
                field_names.extend([f"{field}_{i:04d}" for i in range(count)])
                type_names.extend([np_type] * count)

        return np.dtype([x for x in zip(field_names, type_names)])


def _parse_pc_data(fp: TextIO | BinaryIO, metadata: MetaData) -> np.ndarray:
    dtype = metadata.build_dtype()

    if metadata.data == "ascii":
        pc_data = np.loadtxt(fp, dtype=dtype, delimiter=" ")
    elif metadata.data == "binary":
        pc_data = np.frombuffer(
            fp.read(metadata.points * dtype.itemsize), dtype=dtype  # type: ignore
        )
    elif metadata.data == "binary_compressed":
        compressed_size, uncompressed_size = struct.unpack("II", fp.read(8))  # type: ignore

        buffer = lzf.decompress(fp.read(compressed_size), uncompressed_size)
        if (actual_size := len(buffer)) != uncompressed_size:
            raise RuntimeError(
                f"Failed to decompress data. "
                f"Expected decompressed file size is {uncompressed_size}, but got {actual_size}"
            )

        offset = 0
        pc_data = np.zeros(metadata.width, dtype=dtype)
        for name in dtype.names:
            dt: np.dtype = dtype[name]
            bytes = dt.itemsize * metadata.width
            pc_data[name] = np.frombuffer(buffer[offset : (offset + bytes)], dtype=dt)
            offset += bytes
    else:
        raise RuntimeError(
            f"DATA field is neither 'ascii' or 'binary' or 'binary_compressed', "
            f"but got {metadata.data}"
        )

    return pc_data


def _compose_pc_data(points: np.ndarray | Sequence[np.ndarray], metadata: MetaData) -> np.ndarray:
    arrays: Sequence[np.ndarray]
    if isinstance(points, np.ndarray):
        arrays = [points[:, i] for i in range(len(metadata.fields))]
    else:
        arrays = points

    return np.rec.fromarrays(arrays, dtype=metadata.build_dtype())


class PointCloud:
    def __init__(self, metadata: MetaData, pc_data: np.ndarray) -> None:
        self.metadata = metadata
        self.pc_data = pc_data

    @staticmethod
    def from_fileobj(fp: TextIO | BinaryIO) -> PointCloud:
        lines: list[str] = []
        while True:
            line = fp.readline().strip()
            lines.append(line.decode(encoding="utf-8") if isinstance(line, bytes) else line)

            if lines[-1].startswith("DATA"):
                break

        metadata = MetaData.parse_header(lines)
        pc_data = _parse_pc_data(fp, metadata)

        return PointCloud(metadata, pc_data)

    @staticmethod
    def from_path(path: str | Path) -> PointCloud:
        with open(path, mode="rb") as fp:
            return PointCloud.from_fileobj(fp)

    @staticmethod
    def from_points(
        points: np.ndarray | Sequence[np.ndarray],
        fields: Sequence[str],
        types: Sequence[type[np.floating | np.integer] | np.dtype],
        count: Sequence[int] | None = None,
    ) -> PointCloud:
        if not isinstance(points, (np.ndarray, Sequence)):
            raise TypeError(f"Expected np.ndarray or Sequence, but got {type(points)}")

        if count is None:
            count = [1] * len(tuple(fields))

        type_, size = [], []
        for dtype in types:
            t, s = NUMPY_TYPE_TO_PCD_TYPE[np.dtype(dtype)]
            type_.append(t)
            size.append(s)

        num_points = len(points) if isinstance(points, np.ndarray) else len(points[0])

        metadata = MetaData.model_validate(
            {
                "fields": fields,
                "size": size,
                "type": type_,
                "count": count,
                "width": num_points,
                "points": num_points,
            }
        )

        return PointCloud(metadata, _compose_pc_data(points, metadata))

    @staticmethod
    def from_xyz_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z")
        types = (np.float32, np.float32, np.float32)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzi_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity")
        types = (np.float32, np.float32, np.float32, np.float32)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzl_points(
        points: np.ndarray, label_type: type[np.floating | np.integer] = np.float32
    ) -> PointCloud:
        fields = ("x", "y", "z", "label")
        types = (np.float32, np.float32, np.float32, label_type)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzrgb_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "rgb")
        types = (np.float32, np.float32, np.float32, np.float32)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzrgbl_points(
        points: np.ndarray, label_type: type[np.floating | np.integer] = np.float32
    ) -> PointCloud:
        fields = ("x", "y", "z", "rgb", "label")
        types = (np.float32, np.float32, np.float32, np.float32, label_type)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzil_points(
        points: np.ndarray, label_type: type[np.floating | np.integer] = np.float32
    ) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "label")
        types = (np.float32, np.float32, np.float32, np.float32, label_type)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzirgb_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "rgb")
        types = (np.float32, np.float32, np.float32, np.float32, np.float32)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzirgbl_points(
        points: np.ndarray, label_type: type[np.floating | np.integer] = np.float32
    ) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "rgb", "label")
        types = (np.float32, np.float32, np.float32, np.float32, np.float32, label_type)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzt_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "sec", "nsec")
        types = (np.float32, np.float32, np.float32, np.uint32, np.uint32)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzir_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "ring")
        types = (np.float32, np.float32, np.float32, np.float32, np.uint16)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzirt_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "ring", "time")
        types = (np.float32, np.float32, np.float32, np.float32, np.uint16, np.float32)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzit_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "timestamp")
        types = (np.float32, np.float32, np.float32, np.float32, np.float64)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzis_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "stamp")
        types = (np.float32, np.float32, np.float32, np.float32, np.float64)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzisc_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "stamp", "classification")
        types = (np.float32, np.float32, np.float32, np.float32, np.float64, np.uint8)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzrgbs_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "rgb", "stamp")
        types = (np.float32, np.float32, np.float32, np.float32, np.float64)

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzirgbs_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "rgb", "stamp")
        types = (
            np.float32,
            np.float32,
            np.float32,
            np.float32,
            np.float32,
            np.float64,
        )

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyzirgbsc_points(points: np.ndarray) -> PointCloud:
        fields = ("x", "y", "z", "intensity", "rgb", "stamp", "classification")
        types = (
            np.float32,
            np.float32,
            np.float32,
            np.float32,
            np.float32,
            np.float64,
            np.uint8,
        )

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_xyziradt_points(points: np.ndarray) -> PointCloud:
        fields = (
            "x",
            "y",
            "z",
            "intensity",
            "ring",
            "azimuth",
            "distance",
            "return_type",
            "time_stamp",
        )
        types = (
            np.float32,
            np.float32,
            np.float32,
            np.float32,
            np.uint16,
            np.float32,
            np.float32,
            np.uint8,
            np.float64,
        )

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def from_ouster_points(points: np.ndarray) -> PointCloud:
        fields = (
            "x",
            "y",
            "z",
            "intensity",
            "t",
            "reflectivity",
            "ring",
            "ambient",
            "range",
        )
        types = (
            np.float32,
            np.float32,
            np.float32,
            np.float32,
            np.float32,
            np.uint16,
            np.uint8,
            np.uint16,
            np.uint32,
        )

        return PointCloud.from_points(points, fields, types)

    @staticmethod
    def encode_rgb(rgb: np.ndarray) -> np.ndarray:
        """
        Encode Nx3 uint8 array with RGB values to
        Nx1 float32 array with bit-packed RGB
        """

        rgb_u32 = rgb.astype(np.uint32)

        return np.array(
            (rgb_u32[:, 0] << 16) | (rgb_u32[:, 1] << 8) | (rgb_u32[:, 2] << 0), dtype=np.uint32
        )

    @staticmethod
    def decode_rgb(rgb: np.ndarray) -> np.ndarray:
        """
        Decode Nx1 float32 array with bit-packed RGB to
        Nx3 uint8 array with RGB values
        """

        rgb = rgb.copy().astype(np.uint32)
        r = np.asarray((rgb >> 16) & 255, dtype=np.uint8)[:, None]
        g = np.asarray((rgb >> 8) & 255, dtype=np.uint8)[:, None]
        b = np.asarray(rgb & 255, dtype=np.uint8)[:, None]

        return np.hstack((r, g, b))

    @property
    def fields(self) -> Sequence[str]:
        return self.metadata.fields

    @property
    def types(self) -> Sequence[type[np.floating | np.integer]]:
        return [PCD_TYPE_TO_NUMPY_TYPE[ts] for ts in zip(self.metadata.type, self.metadata.size)]

    @property
    def count(self) -> int:
        return self.metadata.points

    def numpy(self, fields: Sequence[str] | None = None) -> np.ndarray:
        """Convert to (N, M) numpy.ndarray points"""

        if fields is None:
            fields = self.fields

        _stack = [self.pc_data[field] for field in self.fields]

        return np.vstack(_stack).T

    def save(self, path: str | Path) -> None:
        self.metadata.data = "binary_compressed"  # only supports binary_compressed

        with open(path, mode="wb") as fp:
            header = self.metadata.compose_header().encode()
            fp.write(header)

            uncompresseds = [
                np.ascontiguousarray(self.pc_data[field]).tobytes()
                for field in self.pc_data.dtype.names
            ]
            uncompressed = b"".join(uncompresseds)

            if (compressed := lzf.compress(uncompressed)) is None:
                compressed = uncompressed

            fp.write(struct.pack("II", len(compressed), len(uncompressed)))
            fp.write(compressed)
