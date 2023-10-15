import os
from pathlib import Path
from typing import List, Tuple, Union

import cv2
import kornia as K
import kornia.feature as KF
import numpy as np
import torch
from easydict import EasyDict as edict


from .deep_image_matcher.thirdparty.SuperGlue.models.matching import Matching
from .deep_image_matcher.thirdparty.LightGlue.lightglue import SuperPoint
from .deep_image_matcher.thirdparty.LightGlue.lightglue.utils import load_image
from .deep_image_matcher.thirdparty.alike.alike import ALike, configs


class LocalFeatures:
    def __init__(
        self,
        method: str,
        n_features: int,
        cfg: dict = None,
    ) -> None:
        self.n_features = n_features
        self.method = method

        self.kpts = {}
        self.descriptors = {}

        # If method is ALIKE, load Alike model weights
        if self.method == "ALIKE":
            self.alike_cfg = cfg
            self.model = ALike(
                **configs[self.alike_cfg["model"]],
                device=self.alike_cfg["device"],
                top_k=self.alike_cfg["top_k"],
                scores_th=self.alike_cfg["scores_th"],
                n_limit=self.alike_cfg["n_limit"],
            )

        elif self.method == "ORB":
            self.orb_cfg = cfg

        elif self.method == "DISK":
            self.orb_cfg = cfg
            self.device = torch.device("cuda")
            self.disk = KF.DISK.from_pretrained('depth').to(self.device)

        elif self.method == "KeyNetAffNetHardNet":
            self.kornia_cfg = cfg
            self.device = torch.device("cuda")

        elif self.method == "SuperPoint":
            self.kornia_cfg = cfg

    def load_torch_image(self, fname):
        cv_img = cv2.imread(fname)
        img = K.image_to_tensor(cv_img, False).float() / 255.0
        img = K.color.rgb_to_grayscale(K.color.bgr_to_rgb(img))
        return img

    def load_torch_image_rgb(self, fname):
        cv_img = cv2.imread(fname)
        img = K.image_to_tensor(cv_img, False).float() / 255.0
        return img
    
    def ORB(self, images: List[Path]) -> Tuple[List[np.ndarray], List[np.ndarray]]:
        for im_path in images:
            im_path = Path(im_path)
            im = cv2.imread(str(im_path), cv2.IMREAD_GRAYSCALE)
            orb = cv2.ORB_create(
                nfeatures=self.n_features,
                scaleFactor=self.orb_cfg.scaleFactor,
                nlevels=self.orb_cfg.nlevels,
                edgeThreshold=self.orb_cfg.edgeThreshold,
                firstLevel=self.orb_cfg.firstLevel,
                WTA_K=self.orb_cfg.WTA_K,
                scoreType=self.orb_cfg.scoreType,
                patchSize=self.orb_cfg.patchSize,
                fastThreshold=self.orb_cfg.fastThreshold,
            )
            kp = orb.detect(im, None)
            kp, des = orb.compute(im, kp)
            kpts = cv2.KeyPoint_convert(kp)

            one_matrix = np.ones((len(kp), 1))
            kpts = np.append(kpts, one_matrix, axis=1)
            zero_matrix = np.zeros((len(kp), 1))
            kpts = np.append(kpts, zero_matrix, axis=1).astype(np.float32)

            zero_matrix = np.zeros((des.shape[0], 96))
            des = np.append(des, zero_matrix, axis=1).astype(np.float32)
            des = np.absolute(des)
            des = des * 512 / np.linalg.norm(des, axis=1).reshape((-1, 1))
            des = np.round(des)
            des = np.array(des, dtype=np.uint8)

            self.kpts[im_path.stem] = kpts
            self.descriptors[im_path.stem] = des

            laf = None

        return self.kpts, self.descriptors, laf

    def ALIKE(self, images: np.ndarray):
        for img in images:
            features = self.model(img, sub_pixel=self.alike_cfg["subpixel"])
            laf = None
        return features["keypoints"], features["descriptors"], laf


    def DISK(self, images: List[Path]):
        # Inspired by: https://github.com/ducha-aiki/imc2023-kornia-starter-pack/blob/main/DISK-adalam-pycolmap-3dreconstruction.ipynb
        disk = self.disk
        with torch.inference_mode():
            for im_path in images:
                img = self.load_torch_image_rgb(str(im_path)).to(self.device)
                features = disk(img, self.n_features, pad_if_not_divisible=True)[0]
                kps1, descs = features.keypoints, features.descriptors

                self.kpts[im_path.stem] = kps1.cpu().detach().numpy()
                self.descriptors[im_path.stem] = descs.cpu().detach().numpy()

                laf = None

        return self.kpts, self.descriptors, laf

    def SuperPoint(self, images: List[Path]):
        with torch.inference_mode():
            for im_path in images:
                extractor = SuperPoint(max_num_keypoints=self.n_features).eval().cuda()
                image = load_image(im_path).cuda()
                feats = extractor.extract(image)
                kpt = feats['keypoints'].cpu().detach().numpy()
                desc = feats['descriptors'].cpu().detach().numpy()
                self.kpts[im_path.stem] = kpt.reshape(-1, kpt.shape[-1])
                self.descriptors[im_path.stem] = desc.reshape(-1, desc.shape[-1])
                laf = None

        return self.kpts, self.descriptors, laf

    def KeyNetAffNetHardNet(self, images: List[Path]):
        for im_path in images:
            img = self.load_torch_image(str(im_path)).to(self.device)
            keypts = KF.KeyNetAffNetHardNet(
                num_features=self.n_features, upright=True, device=torch.device("cuda")
            ).forward(img)
            laf = keypts[0].cpu().detach().numpy()
            self.kpts[im_path.stem] = keypts[0].cpu().detach().numpy()[-1, :, :, -1]
            self.descriptors[im_path.stem] = keypts[2].cpu().detach().numpy()[-1, :, :]

        return self.kpts, self.descriptors, laf


class LocalFeatureExtractor:
    def __init__(
        self,
        local_feature: str = "ORB",
        local_feature_cfg: dict = None,
        n_features: int = 1024,
    ) -> None:
        
        self.local_feature = local_feature
        self.detector_and_descriptor = LocalFeatures(
            local_feature, n_features, local_feature_cfg
        )

    def run(self, im0, im1) -> None:
        keypoints = []
        descriptors = []
        lafs = []
        for img in [im0, im1]:
            extract = getattr(self.detector_and_descriptor, self.local_feature)
            kpts, descs, laf = extract([img])
            keypoints.append(kpts)
            descriptors.append(descs)
            lafs.append(laf)
        return keypoints, descriptors, lafs