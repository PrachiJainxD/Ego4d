cd ../..

##--------------------------------------------------------------
RUN_FILE=ego4d/internal/human_pose/main.py

# CONFIG=unc_T1_rawal; DEVICES=0,
CONFIG=iu_bike_rawal; DEVICES=0,
# CONFIG=iu_music_rawal; DEVICES=0,

# ##--------------------------------------------------------------
CUDA_VISIBLE_DEVICES=${DEVICES} python main.py --config-name $CONFIG mode=pose2d