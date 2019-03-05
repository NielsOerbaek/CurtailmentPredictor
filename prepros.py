import pymongo
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import normalize
import requests as re
import config

zone_names = ["Core Zone", "Zone 1", "Zone 1A", "Zone 2", "Zone 2A", "Zone 2B", "Zone 3", "Zone 4", "Zone 4A"]
hours_forecast = 3
hours_history = 8
timesteps = hours_history*6
batch_size = 1

client = pymongo.MongoClient("mongodb://"+config.reader_user+":"+config.reader_pw+"@"+config.SERVER+"/sse-data")
db = client["sse-data"]

def getDate(s): return datetime.fromtimestamp(int(s))
def isCurtailed(z): return z["ANM_Operation"] == "YELLOW"
def isStopped(z): return z["ANM_Operation"] == "RED"


def getDemandGen(start=0, end=0):
    if end == 0: end = start+86400
    demand = db["demand"]
    data = dict()

    for s in demand.find({"timestamp": {"$gt": start-1, "$lt": end}}):
        d = getDate(s["timestamp"])
        data[d] = dict()
        data[d]["Demand"] = s["data"][0]["data"][0]
        data[d]["Generation"] = s["data"][2]["data"][1]+s["data"][3]["data"][1]

    df = pd.DataFrame.from_dict(data, orient="index")
    if len(df) == 0: raise ValueError("No data found for dates: "+ str(datetime.fromtimestamp(start)) +" to "+ str(datetime.fromtimestamp(end)))
    return df


def getANMStatus(start=0, end=0, hours=0):
    if end == 0: end = start+86400
    status = db["ANM_status"]
    data = dict()

    for s in status.find({"timestamp": {"$gt": start-1+(hours*3600), "$lt": end+(hours*3600)}}):
        d = getDate(s["timestamp"]) - timedelta(hours=hours)
        zs = dict()
        for z in zone_names:
            ss = int(isStopped(s[z]))
            cs = int(isCurtailed(s[z]) or ss)
            zs[z] = cs
        data[d] = zs

    df = pd.DataFrame.from_dict(data, orient="index")
    if len(df) == 0: raise ValueError("No data found for dates: "+ str(datetime.fromtimestamp(start)) +" to "+ str(datetime.fromtimestamp(end)))
    return df


def getWeather(start=0, end=0, hours=0):
    if end == 0: end = start+86400
    weather = db["weather"]
    data = dict()

    for w in weather.find({"dt": {"$gt": start-1+(hours*3600), "$lt": end+(hours*3600)}}):
        time = getDate(w["dt"]) - timedelta(hours=hours)
        data[time] = weatherToDict(w)

    df = pd.DataFrame.from_dict(data, orient="index")
    if len(df) == 0: raise ValueError("No data found for dates: "+ str(datetime.fromtimestamp(start)) +" to "+ str(datetime.fromtimestamp(end)))
    return df

def weatherToDict(w):
    d = dict()
    d["speed"] = w["wind"]["speed"]
    if "deg" in w["wind"].keys(): d["deg"] = w["wind"]["deg"]
    else: d["deg"] = -1
    d["pressure"] = w["main"]["pressure"]
    d["temp"] = round(w["main"]["temp"]-273.15,2) # Conversion from Kelvin to Celcius
    return d


def makeDataset(start, stop, hours_forecast=3, norms=None):
    start = datetime.strptime(start, '%Y-%m-%d').timestamp()
    stop = datetime.strptime(stop, '%Y-%m-%d').timestamp()
    if stop <= start: raise ValueError("Invalid date range: "+ str(datetime.fromtimestamp(start)) +" to "+ str(datetime.fromtimestamp(stop)))

    # Get dataframes and resample them.
    demgen_df = getDemandGen(start, stop).resample("10min").mean() # Demand Generation data
    hw_df = getWeather(start, stop, 0).resample("10min").mean() # Historical Weather data
    f_df = getWeather(start, stop, hours_forecast).resample("10min").mean() # Weather Forecast data
    y_df = getANMStatus(start, stop, hours_forecast).resample("10min").max() # Truth Labels
    # NOTE: You could also add previous ANM zone status' to the train input. Maybe that could make sense.

    # Join DemGen-data, with weather- and time-data and average over readings
    h_df = pd.DataFrame(index=pd.date_range(start=datetime.fromtimestamp(start), end=datetime.fromtimestamp(stop), freq='10min', closed="left"))
    h_df = h_df.join(demgen_df).join(hw_df)
    f_df = f_df

    # Add relevant time and date information to forecast data
    f_df["hour"] = [d.hour+1 for d in f_df.index]
    f_df["day"] = [d.day+1 for d in f_df.index]
    f_df["month"] = [d.month+1 for d in f_df.index]
    f_df["weekday"] = [d.weekday()+1 for d in f_df.index]
    # Align x and y by dropping times that are in one but not the other
    y_df, xh_df = y_df.align(h_df, join="inner", axis=0, method="pad")
    xh_df, xf_df = xh_df.align(f_df, join="left", axis=0, method="pad")
    # Fill in possible missing values
    xh_df = xh_df.ffill().bfill()
    xf_df = xf_df.ffill().bfill()

    # Go from dataframes to numpy arrays
    xh, xf, y = xh_df.values, xf_df.values, y_df.values

    # Normalize
    if norms is None:
        # Normalize data and return norms
        xh, h_norms = normalizeData(xh)
        xf, f_norms = normalizeData(xf)
    else:
        # Unpack norms and apply to data
        h_norms, f_norms = norms
        xh = xh / h_norms
        xf = xf / f_norms

    # Make the timeseries from the historical data
    xts, xf, y = makeTimeseries(xh, xf, y)
    yr = reduceZones(y) # Make the reduced labels as well

    return xts, h_norms, xf, f_norms, y, yr


def makeTimeseries(x_h,x_f,y):
    samples = len(x_h)-timesteps+1
    ts = np.zeros(shape=(samples, timesteps, x_h.shape[1]))
    for i in range(samples):
        ts[i] = x_h[i:i+timesteps]
    return ts, x_f[timesteps-1:], y[timesteps-1:]

def splitData(ts,f,y,yr): return train_test_split(ts,f,y,yr, test_size=0.1, shuffle=False)
def normalizeData(x): return normalize(x, axis=0, norm="l2", return_norm=True)
def reduceZones(y): return np.maximum.reduce(y, 1, initial=0)[:,np.newaxis]
def rebuildDate(x): return datetime(datetime.now().year, int(x[-2])-1, int(x[-3])-1, int(x[-4])-1) + timedelta(hours=hours_forecast)

def getForecastData():
    forecasts = re.get("http://api.openweathermap.org/data/2.5/forecast?lat=59.1885692&lon=-2.8229873&APPID="+config.API_KEY).json()
    forecast = forecasts["list"][0]
    dt = getDate(forecast["dt"])
    data = weatherToDict(forecast)
    data["hour"] = dt.hour+1
    data["day"] = dt.day+1
    data["month"] = dt.month+1
    data["weekday"] = dt.weekday()+1
    xf = np.array(list(data.values()))
    return xf, dt

def getLastTimeseries():
    stop = datetime.now()
    stop -= timedelta(minutes=stop.minute%10, seconds=stop.second, microseconds=stop.microsecond)
    start = stop - timedelta(hours=hours_history)
    start_stamp, stop_stamp = start.timestamp(), stop.timestamp()

    demgen = getDemandGen(start_stamp, stop_stamp).resample("10min").mean()
    hw = getWeather(start_stamp, stop_stamp, 0).resample("10min").mean()

    # Join DemGen-data, with weather- and time-data and average over readings
    hist = pd.DataFrame(index=pd.date_range(start=start, end=stop, freq='10min', closed="right"))
    hist = hist.join(demgen).join(hw).ffill().bfill()
    return hist.values

def getPredictionData():
    ts = getLastTimeseries()
    f, fdt = getForecastData()
    if len(ts) != timesteps: raise Exception("Timeseries has " + str(len(ts)) + " values, but should have " + str(timesteps))
    return ts,f,fdt
