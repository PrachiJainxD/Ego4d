import json
import os
import pickle
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import cv2

import hydra

import numpy as np
import pandas as pd
from ego4d.internal.colmap.preprocess import download_andor_generate_streams

from ego4d.internal.human_pose.bbox_detector import DetectorModel
from ego4d.internal.human_pose.camera import (
    batch_xworld_to_yimage,
    create_camera,
    create_camera_data,
    get_aria_camera_models,
)
from ego4d.internal.human_pose.config import Config
from ego4d.internal.human_pose.dataset import (
    get_synced_timesync_df,
    SyncedEgoExoCaptureDset,
)
from ego4d.internal.human_pose.pose_estimator import PoseModel
from ego4d.internal.human_pose.readers import read_frame_idx_set
from ego4d.internal.human_pose.triangulator import Triangulator
from ego4d.internal.human_pose.utils import (
    check_and_convert_bbox,
    draw_bbox_xyxy,
    draw_points_2d,
    get_exo_camera_plane,
    get_region_proposal,
)

from iopath.common.file_io import PathManager
from iopath.common.s3 import S3PathHandler
from tqdm.auto import tqdm

pathmgr = PathManager()
pathmgr.register_handler(S3PathHandler(profile="default"))


@dataclass
class Context:
    data_dir: str
    repo_root_dir: str
    cache_dir: str
    cache_rel_dir: str
    metadata_json: str
    dataset_dir: str
    dataset_json_path: str
    dataset_rel_dir: str
    frame_dir: str
    exo_cam_names: List[str]
    bbox_dir: str
    vis_bbox_dir: str
    pose2d_dir: str
    vis_pose2d_dir: str
    pose3d_dir: str
    vis_pose3d_dir: str
    detector_config: str
    detector_checkpoint: str
    pose_config: str
    pose_checkpoint: str
    dummy_pose_config: str
    dummy_pose_checkpoint: str
    human_height: float = 1.5


def _create_json_from_capture_dir(capture_dir: str) -> Dict[str, Any]:
    if capture_dir.endswith("/"):
        capture_dir = capture_dir[0:-1]

    if capture_dir.startswith("s3://"):
        bucket_name = capture_dir.split("s3://")[1].split("/")[0]
        prefix_path = "s3://{bucket_name}"
    else:
        prefix_path = capture_dir

    dirs = capture_dir.split("/")
    take_id = dirs[-1]
    video_source = dirs[-2]
    video_files = pathmgr.ls(os.path.join(capture_dir, "videos/"))

    def _create_video(f):
        device_id = os.path.splitext(os.path.basename(f))[0]
        device_type = "aria" if "aria" in device_id else "gopro"
        is_ego = device_type == "aria"
        has_walkaround = "mobile" in device_id or "aria" in device_id
        s3_path = os.path.join(prefix_path, f)
        return {
            "device_id": device_id,
            "device_type": device_type,
            "is_ego": is_ego,
            "has_walkaround": has_walkaround,
            "s3_path": s3_path,
        }

    return {
        "take_id": take_id,
        "video_source": video_source,
        "ego_id": "aria01",
        "timesync_csv_path": os.path.join(capture_dir, "timesync.csv"),
        "preview_path": os.path.join(capture_dir, "preview.mp4"),
        "videos": [_create_video(f) for f in video_files],
    }


def get_context(config: Config) -> Context:
    if config.inputs.metadata_json_path is not None:
        metadata_json = (
            json.load(pathmgr.open(config.inputs.metadata_json_path))
            if config.inputs.metadata_json_path is not None
            else _create_json_from_capture_dir(config.inputs.input_capture_dir)
        )
    else:
        metadata_json = _create_json_from_capture_dir(config.inputs.capture_data_dir)

    if not config.data_dir.startswith("/"):
        config.data_dir = os.path.join(config.repo_root_dir, config.data_dir)
        print(f"Using data dir: {config.data_dir}")

    data_dir = config.data_dir
    cache_rel_dir = os.path.join(
        "cache",
        f"{metadata_json['video_source']}_{metadata_json['take_id']}",
    )
    cache_dir = os.path.join(
        config.data_dir,
        cache_rel_dir,
    )
    exo_cam_names = [
        x["device_id"]
        for x in metadata_json["videos"]
        if not x["is_ego"] and not x["has_walkaround"]
    ]
    dataset_dir = os.path.join(cache_dir, config.mode_preprocess.dataset_name)

    # pose2d config
    for rel_path_key in ["pose_config", "dummy_pose_config"]:
        rel_path = config.mode_pose2d[rel_path_key]
        abs_path = os.path.join(config.repo_root_dir, rel_path)
        assert (
            os.path.exists(abs_path),
            f"path for {rel_path_key} must be relative to root repo dir ({config.repo_root_dir})",
        )
        config.mode_pose2d[rel_path_key] = abs_path

    # bbox config
    for rel_path_key in ["detector_config"]:
        rel_path = config.mode_bbox[rel_path_key]
        abs_path = os.path.join(config.repo_root_dir, rel_path)
        assert (
            os.path.exists(abs_path),
            f"path for {rel_path_key} must be relative to root repo dir ({config.repo_root_dir})",
        )
        config.mode_bbox[rel_path_key] = abs_path

    return Context(
        data_dir=data_dir,
        repo_root_dir=config.repo_root_dir,
        cache_dir=cache_dir,
        cache_rel_dir=cache_rel_dir,
        metadata_json=metadata_json,
        dataset_dir=dataset_dir,
        dataset_json_path=os.path.join(dataset_dir, "data.json"),
        dataset_rel_dir=os.path.join(
            cache_rel_dir, config.mode_preprocess.dataset_name
        ),
        frame_dir=os.path.join(dataset_dir, "frames"),
        exo_cam_names=exo_cam_names,
        bbox_dir=os.path.join(dataset_dir, "bbox"),
        vis_bbox_dir=os.path.join(dataset_dir, "vis_bbox"),
        pose2d_dir=os.path.join(dataset_dir, "pose2d"),
        vis_pose2d_dir=os.path.join(dataset_dir, "vis_pose2d"),
        pose3d_dir=os.path.join(dataset_dir, "pose3d"),
        vis_pose3d_dir=os.path.join(dataset_dir, "vis_pose3d"),
        detector_config=config.mode_bbox.detector_config,
        detector_checkpoint=config.mode_bbox.detector_checkpoint,
        pose_config=config.mode_pose2d.pose_config,
        pose_checkpoint=config.mode_pose2d.pose_checkpoint,
        dummy_pose_config=config.mode_pose2d.dummy_pose_config,
        dummy_pose_checkpoint=config.mode_pose2d.dummy_pose_checkpoint,
        human_height=config.mode_bbox.human_height,
    )


def mode_pose3d(config: Config):
    ctx = get_context(config)

    dset = SyncedEgoExoCaptureDset(
        data_dir=config.data_dir,
        dataset_json_path=ctx.dataset_json_path,
        read_frames=False,
    )

    pose_model = PoseModel(
        pose_config=ctx.dummy_pose_config, pose_checkpoint=ctx.dummy_pose_checkpoint
    )  # lightweight for visualization only!

    exo_cameras = {
        exo_camera_name: create_camera(dset[0][exo_camera_name]["camera_data"], None)
        for exo_camera_name in ctx.exo_cam_names
    }
    if not os.path.exists(ctx.pose3d_dir):
        os.makedirs(ctx.pose3d_dir)

    os.makedirs(ctx.vis_pose3d_dir, exist_ok=True)

    poses3d = {}

    pose2d_file = os.path.join(ctx.pose2d_dir, "pose2d.pkl")
    assert os.path.exists(pose2d_file), f"{pose2d_file} does not exist"
    with open(pose2d_file, "rb") as f:
        poses2d = pickle.load(f)

    for time_stamp in tqdm(range(len(dset)), total=len(dset)):
        info = dset[time_stamp]

        multi_view_pose2d = {
            exo_camera_name: poses2d[time_stamp][exo_camera_name]
            for exo_camera_name in ctx.exo_cam_names
        }

        # triangulate
        triangulator = Triangulator(
            time_stamp, ctx.exo_cam_names, exo_cameras, multi_view_pose2d
        )
        pose3d = triangulator.run(debug=True)  ## 17 x 4 (x, y, z, confidence)
        poses3d[time_stamp] = pose3d

        # visualize pose3d
        for exo_camera_name in ctx.exo_cam_names:
            image_path = info[exo_camera_name]["abs_frame_path"]
            image = cv2.imread(image_path)
            exo_camera = exo_cameras[exo_camera_name]

            vis_pose3d_cam_dir = os.path.join(ctx.vis_pose3d_dir, exo_camera_name)
            if not os.path.exists(vis_pose3d_cam_dir):
                os.makedirs(vis_pose3d_cam_dir)

            projected_pose3d = batch_xworld_to_yimage(pose3d[:, :3], exo_camera)
            projected_pose3d = np.concatenate(
                [projected_pose3d, pose3d[:, 3].reshape(-1, 1)], axis=1
            )  ## 17 x 3

            save_path = os.path.join(vis_pose3d_cam_dir, f"{time_stamp:05d}.jpg")
            pose_model.draw_projected_poses3d([projected_pose3d], image, save_path)

    with open(os.path.join(ctx.pose3d_dir, "pose3d.pkl"), "wb") as f:
        pickle.dump(poses3d, f)


def mode_pose2d(config: Config):
    ctx = get_context(config)

    dset = SyncedEgoExoCaptureDset(
        data_dir=config.data_dir,
        dataset_json_path=ctx.dataset_json_path,
        read_frames=False,
    )

    pose_model = PoseModel(
        pose_config=ctx.pose_config, pose_checkpoint=ctx.pose_checkpoint
    )

    # construct ground plane, it is parallel to the plane with all gopro camera centers----------------
    if not os.path.exists(ctx.pose2d_dir):
        os.makedirs(ctx.pose2d_dir)

    ## if ctx.vis_pose_dir does not exist make it
    if not os.path.exists(ctx.vis_pose2d_dir):
        os.makedirs(ctx.vis_pose2d_dir)

    poses2d = {}

    # load bboxes from bbox_dir/bbox.pkl
    bbox_file = os.path.join(ctx.bbox_dir, "bbox.pkl")

    if not os.path.exists(bbox_file):
        print(f"bbox path does not exist: {bbox_file}")
        print("NOTE: run mode_bbox")
        sys.exit(1)

    with open(bbox_file, "rb") as f:
        bboxes = pickle.load(f)

    for time_stamp in tqdm(range(len(dset)), total=len(dset)):
        info = dset[time_stamp]

        poses2d[time_stamp] = {}

        for exo_camera_name in ctx.exo_cam_names:
            image_path = info[exo_camera_name]["abs_frame_path"]
            image = cv2.imread(image_path)

            vis_pose2d_cam_dir = os.path.join(ctx.vis_pose2d_dir, exo_camera_name)
            if not os.path.exists(vis_pose2d_cam_dir):
                os.makedirs(vis_pose2d_cam_dir)

            bbox_xyxy = bboxes[time_stamp][exo_camera_name]  # x1, y1, x2, y2

            if bbox_xyxy is not None:
                # add confidence score to the bbox
                bbox_xyxy = np.append(bbox_xyxy, 1.0)

                pose_results = pose_model.get_poses2d(
                    bboxes=[{"bbox": bbox_xyxy}],
                    image_name=image_path,
                )

                assert len(pose_results) == 1

                save_path = os.path.join(vis_pose2d_cam_dir, f"{time_stamp:05d}.jpg")
                pose_model.draw_poses2d(pose_results, image, save_path)

                pose_result = pose_results[0]
                pose2d = pose_result["keypoints"]

            else:
                pose2d = None
                save_path = os.path.join(vis_pose2d_cam_dir, f"{time_stamp:05d}.jpg")
                cv2.imwrite(save_path, image)

            poses2d[time_stamp][exo_camera_name] = pose2d

    # save poses2d to pose2d_dir/pose2d.pkl
    with open(os.path.join(ctx.pose2d_dir, "pose2d.pkl"), "wb") as f:
        pickle.dump(poses2d, f)


def mode_bbox(config: Config):
    ctx = get_context(config)

    dset = SyncedEgoExoCaptureDset(
        data_dir=config.data_dir,
        dataset_json_path=ctx.dataset_json_path,
        read_frames=False,
    )

    detector_model = DetectorModel(
        detector_config=ctx.detector_config,
        detector_checkpoint=ctx.detector_checkpoint,
    )

    # construct ground plane, it is parallel to the plane with all gopro camera centers
    exo_cameras = {
        exo_camera_name: create_camera(dset[0][exo_camera_name]["camera_data"], None)
        for exo_camera_name in ctx.exo_cam_names
    }
    exo_camera_centers = np.array(
        [exo_camera.center for exo_camera_name, exo_camera in exo_cameras.items()]
    )
    _, camera_plane_unit_normal = get_exo_camera_plane(exo_camera_centers)

    os.makedirs(ctx.bbox_dir, exist_ok=True)
    os.makedirs(ctx.vis_bbox_dir, exist_ok=True)

    bboxes = {}

    for time_stamp in tqdm(range(len(dset)), total=len(dset)):
        info = dset[time_stamp]
        bboxes[time_stamp] = {}

        for exo_camera_name in ctx.exo_cam_names:
            image_path = info[exo_camera_name]["abs_frame_path"]
            image = cv2.imread(image_path)

            vis_bbox_cam_dir = os.path.join(ctx.vis_bbox_dir, exo_camera_name)
            if not os.path.exists(vis_bbox_cam_dir):
                os.makedirs(vis_bbox_cam_dir)

            exo_camera = create_camera(info[exo_camera_name]["camera_data"], None)
            left_camera = create_camera(
                info["aria_slam_left"]["camera_data"], None
            )  ## TODO: use the camera model of the aria camera
            right_camera = create_camera(
                info["aria_slam_right"]["camera_data"], None
            )  ## TODO: use the camera model of the aria camera
            human_center_3d = (left_camera.center + right_camera.center) / 2

            proposal_points_3d = get_region_proposal(
                human_center_3d,
                unit_normal=camera_plane_unit_normal,
                human_height=ctx.human_height,
            )
            proposal_points_2d = batch_xworld_to_yimage(proposal_points_3d, exo_camera)
            proposal_bbox = check_and_convert_bbox(
                proposal_points_2d,
                exo_camera.camera_model.width,
                exo_camera.camera_model.height,
            )

            bbox_xyxy = None
            offshelf_bboxes = None

            if proposal_bbox is not None:
                proposal_bbox = np.array(
                    [
                        proposal_bbox[0],
                        proposal_bbox[1],
                        proposal_bbox[2],
                        proposal_bbox[3],
                        1,
                    ]
                )  ## add confidnece
                proposal_bboxes = [{"bbox": proposal_bbox}]
                offshelf_bboxes = detector_model.get_bboxes(
                    image_name=image_path, bboxes=proposal_bboxes
                )

            if offshelf_bboxes is not None:
                assert len(offshelf_bboxes) == 1  ## single human per scene
                bbox_xyxy = offshelf_bboxes[0]["bbox"][:4]

                # uncomment to visualize the bounding box
                # bbox_image = draw_points_2d(image, proposal_points_2d, radius=5, color=(0, 255, 0))
                bbox_image = draw_bbox_xyxy(image, bbox_xyxy, color=(0, 255, 0))
            #
            else:
                bbox_image = image

            # bbox_image = draw_points_2d(image, proposal_points_2d, radius=5, color=(0, 255, 0))

            cv2.imwrite(
                os.path.join(vis_bbox_cam_dir, f"{time_stamp:05d}.jpg"), bbox_image
            )
            bboxes[time_stamp][exo_camera_name] = bbox_xyxy

    # save the bboxes as a pickle file
    with open(os.path.join(ctx.bbox_dir, "bbox.pkl"), "wb") as f:
        pickle.dump(bboxes, f)


def mode_preprocess(config: Config):
    """
    Does the following:
    - extract all frames for all cameras
    """
    ctx = get_context(config)
    assert config.mode_preprocess.download_video_files, "must download files"
    by_dev_id = download_andor_generate_streams(
        metadata=ctx.metadata_json,
        download_video_files=config.mode_preprocess.download_video_files,
        force_download=config.mode_preprocess.force_download,
        output_dir=ctx.cache_dir,
    )

    shutil.rmtree(ctx.frame_dir, ignore_errors=True)
    os.makedirs(ctx.frame_dir, exist_ok=True)
    i1 = config.inputs.from_frame_number
    i2 = config.inputs.to_frame_number

    synced_df = get_synced_timesync_df(ctx.metadata_json)
    aria_stream_ks = [
        f"aria01_{stream_id}_capture_timestamp_ns"
        for stream_id in config.inputs.aria_streams
    ]
    aria_t1 = min(synced_df[aria_stream_ks].iloc[i1]) / 1e9 - 1 / 30
    aria_t2 = max(synced_df[aria_stream_ks].iloc[i2]) / 1e9 + 1 / 30

    # aria
    aria_frame_dir = os.path.join(ctx.frame_dir, "aria01")
    os.makedirs(aria_frame_dir, exist_ok=True)
    print("Extracting aria")
    cmd = [
        config.mode_preprocess.vrs_bin_path,
        "extract-all",
        by_dev_id["aria01"]["local_path"],
    ]
    if not config.mode_preprocess.extract_all_aria_frames:
        cmd += [
            "--after",
            str(aria_t1),
            "--before",
            str(aria_t2),
        ]
    cmd += [
        "--to",
        aria_frame_dir,
    ]
    print("Running:")
    print(" ".join(cmd))
    subprocess.run(cmd)

    # gopro
    for cam in ctx.exo_cam_names:
        cam_frame_dir = os.path.join(ctx.frame_dir, cam)
        os.makedirs(cam_frame_dir, exist_ok=True)
        frame_indices = [
            int(x) for x in synced_df[f"{cam}_frame_number"].iloc[i1 : i2 + 1].tolist()
        ]
        frames = read_frame_idx_set(
            path=by_dev_id[cam]["local_path"],
            frame_indices=frame_indices,
            stream_id=0,
        )
        for idx, frame in tqdm(frames, total=len(frame_indices)):
            out_path = os.path.join(cam_frame_dir, f"{idx:06d}.jpg")
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            assert cv2.imwrite(out_path, frame), out_path

    # TODO: do as separate processing step
    print("Normalizing pose data")
    aria_frames_by_stream = {}
    for stream_id in config.inputs.aria_streams:
        aria_rel_frames = [
            f
            for f in os.listdir(os.path.join(aria_frame_dir, stream_id))
            if f.endswith("jpg")
        ]

        aria_frame_to_info = {
            ".".join(f.split("-")[-1].split(".")[0:2]): {
                "t": float(".".join(f.split("-")[-1].split(".")[0:2])),
                "path": os.path.join("aria01", stream_id, f),
            }
            for f in aria_rel_frames
        }
        aria_frames_by_stream[stream_id] = aria_frame_to_info

    aria_traj_df = pd.read_csv(pathmgr.open(config.inputs.aria_trajectory_path))
    exo_traj_df = pd.read_csv(pathmgr.open(config.inputs.exo_trajectory_path))
    output = []

    stream_id_to_name = {
        "211-1": "aria_et",
        "214-1": "aria_rgb",
        "1201-1": "aria_slam_left",
        "1201-2": "aria_slam_right",
    }

    aria_path = by_dev_id["aria01"]["local_path"]
    assert not aria_path.startswith("https:") or not aria_path.startswith("s3:")
    aria_camera_models = get_aria_camera_models(aria_path)
    for idx in tqdm(range(i1, i2 + 1), total=i2 - i1 + 1):
        row = {}
        row_df = synced_df.iloc[idx]
        for stream_id in config.inputs.aria_streams:
            frame_num = int(row_df[f"aria01_{stream_id}_frame_number"])
            frame_t = row_df[f"aria01_{stream_id}_capture_timestamp_ns"] / 1e9
            frame_t = f"{frame_t:.3f}"
            assert frame_t in aria_frames_by_stream[stream_id]
            a_info = aria_frames_by_stream[stream_id][frame_t]
            aria_t = a_info["t"]
            aria_pose = aria_traj_df.iloc[
                (aria_traj_df.tracking_timestamp_us - aria_t * 1e6)
                .abs()
                .argsort()
                .iloc[0]
            ].to_dict()
            row[stream_id_to_name[stream_id]] = {
                "frame_path": a_info["path"],
                "frame_number": idx,
                "t": aria_t,
                "camera_data": create_camera_data(
                    device_row=aria_pose,
                    name=stream_id,
                    camera_model=aria_camera_models[stream_id],
                    device_row_key="device",
                ),
                "_raw_camera": aria_pose,
            }

        for cam_id in ctx.exo_cam_names:
            frame_num = int(row_df[f"{cam_id}_frame_number"])
            frame_path = os.path.join(cam_id, f"{frame_num:06d}.jpg")
            exo_name = (
                config.inputs.exo_timesync_name_to_calib_name[cam_id]
                if config.inputs.exo_timesync_name_to_calib_name
                else cam_id
            )
            # TODO: matching on tracking_timestamp_us when it is supported
            cam_data = exo_traj_df[exo_traj_df.gopro_uid == exo_name].iloc[0].to_dict()

            row[cam_id] = {
                "frame_path": frame_path,
                "frame_number": frame_num,
                "t": None,
                "camera_data": create_camera_data(
                    device_row=cam_data,
                    name=cam_id,
                    camera_model=None,
                    device_row_key="gopro",
                ),
                "_raw_camera": cam_data,
            }
        output.append(row)

    dataset_json = {
        "cache_dir": ctx.cache_rel_dir,
        "dataset_dir": ctx.dataset_rel_dir,
        "frames": output,
    }
    json.dump(dataset_json, open(ctx.dataset_json_path, "w"))


def mode_multi_view_vis(config: Config):
    ctx = get_context(config)
    camera_names = ctx.exo_cam_names

    read_dir = ctx.vis_pose3d_dir
    write_dir = os.path.join(ctx.vis_pose3d_dir, "multi_view")
    os.makedirs(write_dir, exist_ok=True)

    write_image_width = 3840
    write_image_height = 2160

    read_image_width = 3840
    read_image_height = 2160

    fps = 30
    padding = 5

    total_width_with_padding = 2 * read_image_width + padding
    total_height_with_padding = 2 * read_image_height + padding

    total_width = 2 * read_image_width
    total_height = 2 * read_image_height
    divide_val = 2

    image_names = [
        image_name
        for image_name in sorted(os.listdir(os.path.join(read_dir, camera_names[0])))
        if image_name.endswith(".jpg")
    ]

    for t, image_name in enumerate(tqdm(image_names)):
        canvas = 255 * np.ones((total_height_with_padding, total_width_with_padding, 3))

        for idx, camera_name in enumerate(camera_names):
            camera_image = cv2.imread(os.path.join(read_dir, camera_name, image_name))
            camera_image = cv2.resize(
                camera_image, (read_image_width, read_image_height)
            )

            ##------------paste-----------------
            col_idx = idx % divide_val
            row_idx = idx // divide_val

            origin_x = read_image_width * col_idx + col_idx * padding
            origin_y = read_image_height * row_idx + row_idx * padding
            image = camera_image

            canvas[
                origin_y : origin_y + image.shape[0],
                origin_x : origin_x + image.shape[1],
                :,
            ] = image[:, :, :]

        ##---------resize to target size, ffmpeg does not work with offset image sizes---------
        canvas = cv2.resize(canvas, (total_width, total_height))
        canvas = cv2.resize(canvas, (write_image_width, write_image_height))

        cv2.imwrite(os.path.join(write_dir, image_name), canvas)

    ##----------make video--------------
    command = "rm -rf {}/exo.mp4".format(write_dir)
    os.system(command)

    command = (
        "ffmpeg -r {} -f image2 -i {}/%05d.jpg -pix_fmt yuv420p {}/exo.mp4".format(
            fps, write_dir, ctx.vis_pose3d_dir
        )
    )
    os.system(command)


@hydra.main(config_path="configs", config_name=None)
def run(config: Config):
    if config.mode == "preprocess":
        mode_preprocess(config)
    elif config.mode == "bbox":
        mode_bbox(config)
    elif config.mode == "pose2d":
        mode_pose2d(config)
    elif config.mode == "pose3d":
        mode_pose3d(config)
    elif config.mode == "multi_view_vis":
        mode_multi_view_vis(config)
    else:
        raise AssertionError(f"unknown mode: {config.mode}")


if __name__ == "__main__":
    run()  # pyre-ignore
